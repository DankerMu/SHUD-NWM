"""Closed nested validator for the four-lane performance receipt."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from packages.common.evidence_io import BoundedEvidenceError, reject_secret_material
from packages.common.node27_issue1895_dsn import READONLY_ROLE
from packages.common.node27_issue1895_lanes import (
    COLD_STATE,
    HOT_STATE,
    LANE_NAMES,
    lane_kind,
    lane_source,
)
from packages.common.node27_issue1895_percentiles import (
    ACCEPTED_SAMPLE_COUNT,
    API_P95_LIMIT_MS,
    P95_NEAREST_RANK_INDEX,
    SQL_P95_LIMIT_MS,
    WARMUP_COUNT,
    nearest_rank_p95,
)
from packages.common.node27_issue1895_performance import (
    ARTIFACT,
    FORECAST_SERIES_PATH_PREFIX,
    FORECAST_SERIES_PATH_SUFFIX,
    HTTP_BODY_LIMIT_BYTES,
    HTTP_TIMEOUT_SECONDS,
    PERCENTILE_METHOD,
    SCHEMA_VERSION,
    assert_forecast_series_path,
)
from packages.common.node27_issue1895_query import timeseries_segment_id
from packages.common.node27_issue1895_sql import PLAN_BUFFER_LIMIT, normalize_candidate_chunk_name
from packages.common.node27_issue1895_types import Issue1895ReadinessError

BROWSER_LANE = "test:e2e:live-river-click"
BROWSER_LIMIT_MS = 2000
BROWSER_COMPARATOR = "strict_lt"
BROWSER_BINDER = "apps/frontend/scripts/river-click-receipt-binder.mjs"

REQUIRED_RECEIPT_KEYS = (
    "artifact",
    "schema_version",
    "status",
    "failure",
    "lanes",
    "frozen",
    "browser",
    "identity",
    "percentile_method",
    "readonly",
)
REQUIRED_IDENTITY_KEYS = (
    "head_sha",
    "reviewed_sha",
    "api_origin",
    "timeout_seconds",
    "body_limit_bytes",
    "artifact",
    "basin_id",
    "segment_id",
)
REQUIRED_READONLY_KEYS = ("transaction_read_only", "current_user")
REQUIRED_BROWSER_KEYS = ("lane", "p95_limit_ms", "comparator", "binder")
REQUIRED_LANE_KEYS = (
    "name",
    "kind",
    "source",
    "state",
    "sql",
    "api",
    "run_id",
    "model_id",
    "cycle_time",
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "source_id",
    "scenario",
    "segment_id",
    "timeseries_segment_id",
    "window_start",
    "window_end",
    "api_path",
    "api_query",
    "query_digest",
    "candidate_chunk_names",
)
REQUIRED_FROZEN_KEYS = (
    "run_id",
    "model_id",
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "cycle_time",
    "source_id",
    "scenario",
    "segment_id",
    "timeseries_segment_id",
    "window_start",
    "window_end",
    "api_path",
    "api_query",
    "query_digest",
    "state",
    "candidate_chunk_names",
)
REQUIRED_SQL_KEYS = (
    "warmup_count",
    "accepted_count",
    "percentile_method",
    "p95_index",
    "p95_ms",
    "p95_limit_ms",
    "buffers",
    "buffer_limit",
    "seq_scan",
    "all_chunk_decompression",
    "hypertable",
    "segment_id",
    "window_start",
    "window_end",
    "candidate_count",
    "accepted_durations_ms",
    "plan_metrics",
)
REQUIRED_API_KEYS = (
    "warmup_count",
    "accepted_count",
    "percentile_method",
    "p95_index",
    "p95_ms",
    "p95_limit_ms",
    "path",
    "timeout_seconds",
    "body_limit_bytes",
    "accepted_durations_ms",
    "accepted_statuses",
)
REQUIRED_PLAN_METRIC_KEYS = (
    "index",
    "shared_hit_blocks",
    "shared_read_blocks",
    "shared_buffer_blocks",
    "decompressed_count",
    "touched_count",
    "candidate_count",
    "actual_rows",
)


def _closed(mapping: Mapping[str, Any], keys: Sequence[str], *, code: str) -> None:
    if set(mapping) != set(keys):
        raise Issue1895ReadinessError(
            "nested receipt object keys are not closed",
            code=code,
            stage="performance",
        )


def _require_int(value: object, *, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Issue1895ReadinessError(
            "performance receipt integer field is invalid",
            code=code,
            stage="performance",
        )
    return value


def _require_finite(value: object, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise Issue1895ReadinessError(
            "performance receipt number is not finite",
            code=code,
            stage="performance",
        )
    return float(value)


def _require_sha(value: object) -> str:
    text = str(value or "")
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise Issue1895ReadinessError(
            "performance receipt SHA is invalid",
            code="RECEIPT_SHA_INVALID",
            stage="performance",
        )
    return text


def _require_durations(values: object, *, count: int) -> list[float]:
    if not isinstance(values, list) or len(values) != count:
        raise Issue1895ReadinessError(
            "accepted durations array length drifted",
            code="RECEIPT_DURATION_COUNT",
            stage="performance",
        )
    out: list[float] = []
    for item in values:
        out.append(_require_finite(item, code="RECEIPT_DURATION_INVALID"))
        if out[-1] < 0:
            raise Issue1895ReadinessError(
                "accepted duration is negative",
                code="RECEIPT_DURATION_INVALID",
                stage="performance",
            )
    return out


def validate_performance_receipt(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed four-lane nested validator. Extra or missing nested keys fail."""

    if not isinstance(document, Mapping):
        raise Issue1895ReadinessError(
            "performance receipt is not an object",
            code="RECEIPT_KEYS_INVALID",
            stage="performance",
        )
    _closed(document, REQUIRED_RECEIPT_KEYS, code="RECEIPT_KEYS_INVALID")
    if document.get("artifact") != ARTIFACT or document.get("schema_version") != SCHEMA_VERSION:
        raise Issue1895ReadinessError(
            "performance receipt artifact/schema drifted",
            code="RECEIPT_SCHEMA_INVALID",
            stage="performance",
        )
    if document.get("percentile_method") != PERCENTILE_METHOD:
        raise Issue1895ReadinessError(
            "performance receipt percentile method drifted",
            code="RECEIPT_METHOD_INVALID",
            stage="performance",
        )
    status = document.get("status")
    if status not in {"PASS", "FAIL", "BLOCKED"}:
        raise Issue1895ReadinessError(
            "performance receipt status is closed",
            code="RECEIPT_STATUS_INVALID",
            stage="performance",
        )
    if status == "PASS" and document.get("failure") is not None:
        raise Issue1895ReadinessError(
            "PASS receipt cannot carry a failure",
            code="RECEIPT_STATUS_INVALID",
            stage="performance",
        )
    if status != "PASS" and not isinstance(document.get("failure"), Mapping):
        raise Issue1895ReadinessError(
            "non-PASS receipt requires a closed failure",
            code="RECEIPT_FAILURE_MISSING",
            stage="performance",
        )
    readonly = document.get("readonly")
    if not isinstance(readonly, Mapping):
        raise Issue1895ReadinessError(
            "performance receipt is missing readonly proof",
            code="RECEIPT_READONLY_MISSING",
            stage="performance",
        )
    _closed(readonly, REQUIRED_READONLY_KEYS, code="RECEIPT_READONLY_KEYS")
    if readonly.get("transaction_read_only") is not True:
        raise Issue1895ReadinessError(
            "performance receipt did not prove a read-only transaction",
            code="RECEIPT_NOT_READONLY",
            stage="performance",
        )
    if readonly.get("current_user") != READONLY_ROLE:
        raise Issue1895ReadinessError(
            "performance receipt did not prove nhms_display_ro",
            code="RECEIPT_ROLE_INVALID",
            stage="performance",
        )
    identity = document.get("identity")
    if not isinstance(identity, Mapping):
        raise Issue1895ReadinessError(
            "performance receipt identity is missing",
            code="RECEIPT_IDENTITY_INVALID",
            stage="performance",
        )
    _closed(identity, REQUIRED_IDENTITY_KEYS, code="RECEIPT_IDENTITY_KEYS")
    head = _require_sha(identity.get("head_sha"))
    reviewed = _require_sha(identity.get("reviewed_sha"))
    if head != reviewed:
        raise Issue1895ReadinessError(
            "performance receipt head_sha is not REVIEWED_SHA",
            code="RECEIPT_SHA_MISMATCH",
            stage="performance",
        )
    if identity.get("artifact") != ARTIFACT:
        raise Issue1895ReadinessError(
            "performance receipt identity artifact drifted",
            code="RECEIPT_IDENTITY_ARTIFACT",
            stage="performance",
        )
    origin = str(identity.get("api_origin") or "")
    if not origin.startswith("http://127.0.0.1:"):
        raise Issue1895ReadinessError(
            "performance receipt origin is not loopback",
            code="RECEIPT_ORIGIN_INVALID",
            stage="performance",
        )
    if _require_int(identity.get("timeout_seconds"), code="RECEIPT_TIMEOUT") != HTTP_TIMEOUT_SECONDS:
        raise Issue1895ReadinessError(
            "performance receipt timeout drifted",
            code="RECEIPT_TIMEOUT",
            stage="performance",
        )
    if _require_int(identity.get("body_limit_bytes"), code="RECEIPT_BODY_LIMIT") != HTTP_BODY_LIMIT_BYTES:
        raise Issue1895ReadinessError(
            "performance receipt body limit drifted",
            code="RECEIPT_BODY_LIMIT",
            stage="performance",
        )
    browser = document.get("browser")
    if not isinstance(browser, Mapping):
        raise Issue1895ReadinessError(
            "performance receipt browser contract is missing",
            code="RECEIPT_BROWSER_INVALID",
            stage="performance",
        )
    _closed(browser, REQUIRED_BROWSER_KEYS, code="RECEIPT_BROWSER_KEYS")
    if (
        browser.get("lane") != BROWSER_LANE
        or browser.get("p95_limit_ms") != BROWSER_LIMIT_MS
        or browser.get("comparator") != BROWSER_COMPARATOR
        or browser.get("binder") != BROWSER_BINDER
    ):
        raise Issue1895ReadinessError(
            "performance receipt browser contract drifted",
            code="RECEIPT_BROWSER_DRIFT",
            stage="performance",
        )
    lanes = document.get("lanes")
    if not isinstance(lanes, Mapping) or set(lanes) != set(LANE_NAMES):
        raise Issue1895ReadinessError(
            "performance receipt lanes are not the four closed product lanes",
            code="RECEIPT_LANE_SET_INVALID",
            stage="performance",
        )
    frozen = document.get("frozen")
    if not isinstance(frozen, Mapping) or set(frozen) != set(LANE_NAMES):
        raise Issue1895ReadinessError(
            "performance receipt frozen pin is invalid",
            code="RECEIPT_FROZEN_INVALID",
            stage="performance",
        )
    for name in LANE_NAMES:
        lane = lanes.get(name)
        if not isinstance(lane, Mapping):
            raise Issue1895ReadinessError(
                "performance receipt lane is missing",
                code="RECEIPT_LANE_INVALID",
                stage="performance",
            )
        _closed(lane, REQUIRED_LANE_KEYS, code="RECEIPT_LANE_KEYS")
        if lane.get("name") != name:
            raise Issue1895ReadinessError(
                "lane name drifted from the closed key",
                code="RECEIPT_LANE_NAME",
                stage="performance",
            )
        if lane.get("source") != lane_source(name) or lane.get("kind") != lane_kind(name):
            raise Issue1895ReadinessError(
                "lane source/kind drifted from the closed name",
                code="RECEIPT_LANE_IDENTITY",
                stage="performance",
            )
        expected_state = HOT_STATE if lane_kind(name) == "hot" else COLD_STATE
        if lane.get("state") != expected_state:
            raise Issue1895ReadinessError(
                "lane state drifted from the closed kind",
                code="RECEIPT_LANE_STATE",
                stage="performance",
            )
        pin = frozen.get(name)
        if not isinstance(pin, Mapping):
            raise Issue1895ReadinessError(
                "frozen pin is missing",
                code="RECEIPT_FROZEN_INVALID",
                stage="performance",
            )
        _closed(pin, REQUIRED_FROZEN_KEYS, code="RECEIPT_FROZEN_KEYS")
        if pin.get("state") != expected_state:
            raise Issue1895ReadinessError(
                "frozen state drifted from the lane",
                code="RECEIPT_FROZEN_STATE",
                stage="performance",
            )
        sql = lane.get("sql")
        api = lane.get("api")
        if not isinstance(sql, Mapping) or not isinstance(api, Mapping):
            raise Issue1895ReadinessError(
                "performance receipt lane sql/api is missing",
                code="RECEIPT_LANE_INVALID",
                stage="performance",
            )
        _closed(sql, REQUIRED_SQL_KEYS, code="RECEIPT_SQL_KEYS")
        _closed(api, REQUIRED_API_KEYS, code="RECEIPT_API_KEYS")
        if sql.get("seq_scan") is not False:
            raise Issue1895ReadinessError(
                "performance receipt retained Seq Scan",
                code="RECEIPT_SEQ_SCAN",
                stage="performance",
            )
        if sql.get("all_chunk_decompression") is not False:
            raise Issue1895ReadinessError(
                "performance receipt retained all-chunk decompression",
                code="RECEIPT_ALL_CHUNK_DECOMPRESSION",
                stage="performance",
            )
        if sql.get("warmup_count") != WARMUP_COUNT or api.get("warmup_count") != WARMUP_COUNT:
            raise Issue1895ReadinessError(
                "performance receipt warmup_count is not 1",
                code="RECEIPT_WARMUP_COUNT",
                stage="performance",
            )
        if _require_int(sql.get("accepted_count"), code="RECEIPT_SAMPLE_COUNT") != ACCEPTED_SAMPLE_COUNT:
            raise Issue1895ReadinessError(
                "performance receipt SQL accepted_count is not 20",
                code="RECEIPT_SAMPLE_COUNT",
                stage="performance",
            )
        if _require_int(api.get("accepted_count"), code="RECEIPT_SAMPLE_COUNT") != ACCEPTED_SAMPLE_COUNT:
            raise Issue1895ReadinessError(
                "performance receipt API accepted_count is not 20",
                code="RECEIPT_SAMPLE_COUNT",
                stage="performance",
            )
        path = assert_forecast_series_path(str(api.get("path") or ""))
        if not path.startswith(FORECAST_SERIES_PATH_PREFIX) or not path.endswith(FORECAST_SERIES_PATH_SUFFIX):
            raise Issue1895ReadinessError(
                "API path is not the shipping forecast-series contract",
                code="API_PATH_INVALID",
                stage="performance",
            )
        sql_durations = _require_durations(sql.get("accepted_durations_ms"), count=ACCEPTED_SAMPLE_COUNT)
        api_durations = _require_durations(api.get("accepted_durations_ms"), count=ACCEPTED_SAMPLE_COUNT)
        sql_p95 = nearest_rank_p95(sql_durations)
        api_p95 = nearest_rank_p95(api_durations)
        if _require_finite(sql.get("p95_ms"), code="RECEIPT_P95_INVALID") != sql_p95:
            raise Issue1895ReadinessError(
                "SQL p95 is not the nearest-rank recomputation",
                code="RECEIPT_P95_RECOMPUTE",
                stage="performance",
            )
        if _require_finite(api.get("p95_ms"), code="RECEIPT_P95_INVALID") != api_p95:
            raise Issue1895ReadinessError(
                "API p95 is not the nearest-rank recomputation",
                code="RECEIPT_P95_RECOMPUTE",
                stage="performance",
            )
        if sql.get("p95_limit_ms") != SQL_P95_LIMIT_MS or api.get("p95_limit_ms") != API_P95_LIMIT_MS:
            raise Issue1895ReadinessError(
                "p95 limit drifted",
                code="RECEIPT_P95_LIMIT",
                stage="performance",
            )
        if sql.get("p95_index") != P95_NEAREST_RANK_INDEX or api.get("p95_index") != P95_NEAREST_RANK_INDEX:
            raise Issue1895ReadinessError(
                "p95 index drifted",
                code="RECEIPT_P95_INDEX",
                stage="performance",
            )
        if sql.get("percentile_method") != PERCENTILE_METHOD or api.get("percentile_method") != PERCENTILE_METHOD:
            raise Issue1895ReadinessError(
                "percentile method drifted",
                code="RECEIPT_METHOD_INVALID",
                stage="performance",
            )
        if status == "PASS" and sql_p95 > SQL_P95_LIMIT_MS:
            raise Issue1895ReadinessError(
                "SQL p95 exceeds the closed threshold",
                code="RECEIPT_P95_EXCEEDED",
                stage="performance",
            )
        if status == "PASS" and api_p95 > API_P95_LIMIT_MS:
            raise Issue1895ReadinessError(
                "API p95 exceeds the closed threshold",
                code="RECEIPT_P95_EXCEEDED",
                stage="performance",
            )
        statuses = api.get("accepted_statuses")
        if not isinstance(statuses, list) or len(statuses) != ACCEPTED_SAMPLE_COUNT:
            raise Issue1895ReadinessError(
                "accepted statuses array length drifted",
                code="RECEIPT_STATUS_COUNT",
                stage="performance",
            )
        for item in statuses:
            if not isinstance(item, int) or isinstance(item, bool) or item < 200 or item > 299:
                raise Issue1895ReadinessError(
                    "accepted API status is not 2xx",
                    code="RECEIPT_API_STATUS",
                    stage="performance",
                )
        buffers = _require_int(sql.get("buffers"), code="RECEIPT_BUFFERS_INVALID")
        if sql.get("buffer_limit") != PLAN_BUFFER_LIMIT:
            raise Issue1895ReadinessError(
                "buffer limit drifted",
                code="RECEIPT_BUFFER_LIMIT",
                stage="performance",
            )
        if status == "PASS" and buffers > PLAN_BUFFER_LIMIT:
            raise Issue1895ReadinessError(
                "performance receipt buffers exceed the closed threshold",
                code="RECEIPT_BUFFERS_EXCEEDED",
                stage="performance",
            )
        metrics = sql.get("plan_metrics")
        if not isinstance(metrics, list) or len(metrics) != ACCEPTED_SAMPLE_COUNT:
            raise Issue1895ReadinessError(
                "plan metrics array length drifted",
                code="RECEIPT_PLAN_METRICS",
                stage="performance",
            )
        lane_candidates = lane.get("candidate_chunk_names")
        if not isinstance(lane_candidates, list) or not lane_candidates:
            raise Issue1895ReadinessError(
                "lane candidates are missing",
                code="RECEIPT_CANDIDATES_INVALID",
                stage="performance",
            )
        unique_candidates = tuple(dict.fromkeys(normalize_candidate_chunk_name(item) for item in lane_candidates))
        if list(unique_candidates) != list(lane_candidates):
            raise Issue1895ReadinessError(
                "lane candidates are not unique bounded names",
                code="RECEIPT_CANDIDATES_INVALID",
                stage="performance",
            )
        if _require_int(sql.get("candidate_count"), code="RECEIPT_CANDIDATE_COUNT") != len(unique_candidates):
            raise Issue1895ReadinessError(
                "SQL candidate_count drifted from the unique candidate list",
                code="RECEIPT_CANDIDATE_COUNT",
                stage="performance",
            )
        max_metric_buffers = 0
        for index, metric in enumerate(metrics, start=1):
            if not isinstance(metric, Mapping):
                raise Issue1895ReadinessError(
                    "plan metric is not an object",
                    code="RECEIPT_PLAN_METRICS",
                    stage="performance",
                )
            _closed(metric, REQUIRED_PLAN_METRIC_KEYS, code="RECEIPT_PLAN_METRIC_KEYS")
            if metric.get("index") != index:
                raise Issue1895ReadinessError(
                    "plan metric index drifted",
                    code="RECEIPT_PLAN_METRIC_INDEX",
                    stage="performance",
                )
            hit = _require_int(metric.get("shared_hit_blocks"), code="RECEIPT_PLAN_METRIC_BUFFERS")
            read = _require_int(metric.get("shared_read_blocks"), code="RECEIPT_PLAN_METRIC_BUFFERS")
            buffer_blocks = _require_int(metric.get("shared_buffer_blocks"), code="RECEIPT_PLAN_METRIC_BUFFERS")
            if hit + read != buffer_blocks:
                raise Issue1895ReadinessError(
                    "plan metric buffers are not hit plus read",
                    code="RECEIPT_PLAN_METRIC_BUFFERS",
                    stage="performance",
                )
            if buffer_blocks > PLAN_BUFFER_LIMIT:
                raise Issue1895ReadinessError(
                    "plan metric buffers exceed the closed threshold",
                    code="RECEIPT_PLAN_METRIC_BUFFERS",
                    stage="performance",
                )
            actual_rows = _require_int(metric.get("actual_rows"), code="RECEIPT_PLAN_METRIC_ROWS")
            if actual_rows < 1:
                raise Issue1895ReadinessError(
                    "plan metric actual_rows is not positive",
                    code="RECEIPT_PLAN_METRIC_ROWS",
                    stage="performance",
                )
            decompressed = _require_int(metric.get("decompressed_count"), code="RECEIPT_PLAN_METRIC_CHUNKS")
            touched = _require_int(metric.get("touched_count"), code="RECEIPT_PLAN_METRIC_CHUNKS")
            metric_candidates = _require_int(metric.get("candidate_count"), code="RECEIPT_PLAN_METRIC_CHUNKS")
            if metric_candidates != len(unique_candidates):
                raise Issue1895ReadinessError(
                    "plan metric candidate_count drifted",
                    code="RECEIPT_PLAN_METRIC_CHUNKS",
                    stage="performance",
                )
            if decompressed > touched or touched > metric_candidates:
                raise Issue1895ReadinessError(
                    "plan metric chunk counts are inconsistent",
                    code="RECEIPT_PLAN_METRIC_CHUNKS",
                    stage="performance",
                )
            max_metric_buffers = max(max_metric_buffers, buffer_blocks)
        if max_metric_buffers != buffers:
            raise Issue1895ReadinessError(
                "lane buffers is not the max plan metric buffer",
                code="RECEIPT_BUFFERS_MISMATCH",
                stage="performance",
            )
        candidates = pin.get("candidate_chunk_names")
        if not isinstance(candidates, list) or not candidates:
            raise Issue1895ReadinessError(
                "frozen candidates are missing",
                code="RECEIPT_FROZEN_CANDIDATES",
                stage="performance",
            )
        if list(candidates) != list(unique_candidates):
            raise Issue1895ReadinessError(
                "frozen candidates drifted from the lane",
                code="RECEIPT_FROZEN_CANDIDATES",
                stage="performance",
            )
        digest = str(pin.get("query_digest") or "")
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            raise Issue1895ReadinessError(
                "frozen query digest is invalid",
                code="RECEIPT_FROZEN_DIGEST",
                stage="performance",
            )
        if pin.get("query_digest") != lane.get("query_digest"):
            raise Issue1895ReadinessError(
                "frozen query digest drifted from the lane",
                code="RECEIPT_FROZEN_DIGEST",
                stage="performance",
            )
        identity_fields = (
            "run_id",
            "model_id",
            "basin_id",
            "basin_version_id",
            "river_network_version_id",
            "cycle_time",
            "source_id",
            "scenario",
            "segment_id",
            "timeseries_segment_id",
            "window_start",
            "window_end",
            "api_path",
            "api_query",
        )
        for field in identity_fields:
            if pin.get(field) != lane.get(field):
                raise Issue1895ReadinessError(
                    "frozen identity drifted from the lane",
                    code="RECEIPT_FROZEN_IDENTITY",
                    stage="performance",
                )
        if pin.get("source_id") != lane.get("source") or pin.get("source_id") != lane_source(name):
            raise Issue1895ReadinessError(
                "lane source drifted from the closed name",
                code="RECEIPT_LANE_IDENTITY",
                stage="performance",
            )
        if pin.get("timeseries_segment_id") != timeseries_segment_id(str(pin.get("segment_id") or "")):
            raise Issue1895ReadinessError(
                "timeseries segment drifted from the requested segment",
                code="RECEIPT_FROZEN_SEGMENT",
                stage="performance",
            )
        if pin.get("segment_id") != identity.get("segment_id"):
            raise Issue1895ReadinessError(
                "lane segment drifted from the receipt pin",
                code="RECEIPT_FROZEN_SEGMENT",
                stage="performance",
            )
        if pin.get("basin_id") != identity.get("basin_id"):
            raise Issue1895ReadinessError(
                "lane basin drifted from the receipt pin",
                code="RECEIPT_FROZEN_IDENTITY",
                stage="performance",
            )
        if pin.get("api_path") != api.get("path"):
            raise Issue1895ReadinessError(
                "frozen API path drifted from the lane API path",
                code="RECEIPT_API_PATH",
                stage="performance",
            )
        if pin.get("window_start") != sql.get("window_start") or pin.get("window_end") != sql.get("window_end"):
            raise Issue1895ReadinessError(
                "frozen window drifted from the SQL lane",
                code="RECEIPT_WINDOW",
                stage="performance",
            )
        if pin.get("timeseries_segment_id") != sql.get("segment_id"):
            raise Issue1895ReadinessError(
                "SQL segment drifted from the frozen timeseries segment",
                code="RECEIPT_FROZEN_SEGMENT",
                stage="performance",
            )
    try:
        reject_secret_material(document, label="performance receipt")
    except BoundedEvidenceError:
        raise Issue1895ReadinessError(
            "performance receipt contains secret material",
            code="RECEIPT_SECRET",
            stage="performance",
        ) from None
    return dict(document)
