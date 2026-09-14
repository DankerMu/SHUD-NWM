"""Native EXPLAIN plan regressions for the PGDATA workload owner.

These cases live beside ``tests/test_node27_pgdata_workload.py`` so each
module stays under the 1,000-line structural limit. Shared constants and
helpers are reused from that suite; this file does not reimplement them.
"""

from __future__ import annotations

from typing import Any

from packages.common.node27_pgdata_workload_plan import evaluate_explain_json_plan
from tests.test_node27_pgdata_workload import (
    ISSUE,
    RNV,
    TS_SEGMENT,
    WINDOW_END,
    _assert_code,
    _plan,
)

NATIVE_START = "2026-05-03T00:00:00Z"
NATIVE_END = "2026-05-10T00:00:00Z"
NATIVE_WINDOW_COND = (
    "((river_segment_key = $4) AND (variable_e = 'q_down'::hydro.river_variable) "
    "AND (valid_time >= '2026-05-03 00:00:00+00'::timestamp with time zone) "
    "AND (valid_time <= '2026-05-10 00:00:00+00'::timestamp with time zone))"
)
NATIVE_TIME_COND = (
    "((river_segment_key = $4) AND "
    "(valid_time >= '2026-05-03 00:00:00+00'::timestamp with time zone) "
    "AND (valid_time <= '2026-05-10 00:00:00+00'::timestamp with time zone))"
)


def _native_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "candidate_chunk_names": ("_hyper_6_1_chunk",),
        "segment_id": TS_SEGMENT,
        "river_network_version_id": RNV,
        "window_start": NATIVE_START,
        "window_end": NATIVE_END,
    }
    kwargs.update(overrides)
    return kwargs


def _native_initplan(*, segment: str = TS_SEGMENT, network: str = RNV, returns: str = "$4") -> dict[str, Any]:
    return {
        "Node Type": "Index Scan",
        "Relation Name": "river_segment",
        "Parent Relationship": "InitPlan",
        "Subplan Name": f"InitPlan 5 (returns {returns})",
        "Index Cond": (
            f"((river_segment_id = '{segment}'::text) AND "
            f"(river_network_version_id = '{network}'::text))"
        ),
        "Actual Rows": 1,
        "Actual Loops": 1,
        "Shared Read Blocks": 0,
        "Shared Hit Blocks": 2,
    }


def _native_index_access(
    *,
    relation: str,
    index_cond: str,
    actual_rows: int = 2,
    actual_loops: int = 1,
    rows_removed: int = 0,
    shared_hit: int = 2,
) -> dict[str, Any]:
    return {
        "Node Type": "Index Scan",
        "Schema": "_timescaledb_internal",
        "Relation Name": relation,
        "Index Cond": index_cond,
        "Actual Rows": actual_rows,
        "Actual Loops": actual_loops,
        "Shared Read Blocks": 0,
        "Shared Hit Blocks": shared_hit,
        "Rows Removed by Filter": rows_removed,
    }


def _native_index_plan(
    *,
    relation: str,
    index_cond: str,
    segment: str = TS_SEGMENT,
    network: str = RNV,
    actual_rows: int = 2,
    actual_loops: int = 1,
    rows_removed: int = 0,
    root_rows: int | None = 2,
    extra_access: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    access = _native_index_access(
        relation=relation,
        index_cond=index_cond,
        actual_rows=actual_rows,
        actual_loops=actual_loops,
        rows_removed=rows_removed,
    )
    children: list[dict[str, Any]] = [_native_initplan(segment=segment, network=network), access]
    if extra_access is not None:
        children.append(extra_access)
    root: dict[str, Any] = {
        "Node Type": "Nested Loop",
        "Shared Read Blocks": 0,
        "Shared Hit Blocks": 26,
        "Plans": children,
    }
    if root_rows is not None:
        root["Actual Rows"] = root_rows
    return [{"Plan": root}]


def test_native_pg_timestamp_and_surrogate_key_are_accepted() -> None:
    payload = _native_index_plan(relation="_hyper_6_1_chunk", index_cond=NATIVE_WINDOW_COND)
    result = evaluate_explain_json_plan(payload, **_native_kwargs())
    assert result["touched_count"] == 1
    assert result["seq_scan"] is False


def test_native_plan_refuses_wrong_time_surrogate_and_unrelated_chunk() -> None:
    kwargs = _native_kwargs()
    wrong_time = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=(
            "((river_segment_key = $4) AND (valid_time >= '2026-05-04 00:00:00+00'::timestamp with time zone) "
            "AND (valid_time <= '2026-05-10 00:00:00+00'::timestamp with time zone))"
        ),
    )
    _assert_code(lambda: evaluate_explain_json_plan(wrong_time, **kwargs), "PLAN_OUT_OF_WINDOW")
    half_open = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=(
            "((river_segment_key = $4) AND (valid_time >= '2026-05-03 00:00:00+00'::timestamp with time zone) "
            "AND (valid_time < '2026-05-10 00:00:00+00'::timestamp with time zone))"
        ),
    )
    _assert_code(lambda: evaluate_explain_json_plan(half_open, **kwargs), "PLAN_OUT_OF_WINDOW")
    wrong_segment = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_TIME_COND,
        segment="other_segment",
    )
    _assert_code(lambda: evaluate_explain_json_plan(wrong_segment, **kwargs), "PLAN_SEGMENT_UNBOUND")
    missing = [
        {
            "Plan": {
                "Node Type": "Index Scan",
                "Schema": "_timescaledb_internal",
                "Relation Name": "_hyper_6_1_chunk",
                "Index Cond": NATIVE_TIME_COND,
                "Shared Read Blocks": 0,
                "Shared Hit Blocks": 2,
                "Actual Rows": 2,
            }
        }
    ]
    _assert_code(lambda: evaluate_explain_json_plan(missing, **kwargs), "PLAN_SEGMENT_UNBOUND")
    wrong_network = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_TIME_COND,
        network="other-network",
    )
    _assert_code(lambda: evaluate_explain_json_plan(wrong_network, **kwargs), "PLAN_SEGMENT_UNBOUND")
    unrelated = _native_index_plan(relation="_hyper_9_9_chunk", index_cond=NATIVE_TIME_COND)
    _assert_code(lambda: evaluate_explain_json_plan(unrelated, **kwargs), "PLAN_UNRELATED_CHUNK")


def test_compressed_physical_index_uses_initplan_and_seq_scan_still_refuses() -> None:
    initplan = _native_initplan()
    indexed = {
        "Node Type": "Custom Scan",
        "Custom Plan Provider": "DecompressChunk",
        "Schema": "_timescaledb_internal",
        "Relation Name": "_hyper_6_2_chunk",
        "Filter": (
            "((valid_time >= '2026-08-01 00:00:00+00'::timestamp with time zone) AND "
            "(valid_time <= '2026-08-08 00:00:00+00'::timestamp with time zone) AND "
            "(basin_version_key = $3) AND (river_network_version_key = $5) AND "
            "(variable_e = 'q_down'::hydro.river_variable))"
        ),
        "Shared Read Blocks": 0,
        "Shared Hit Blocks": 1,
        "Actual Rows": 8,
        "Actual Loops": 1,
        "Plans": [
            {
                "Node Type": "Index Scan",
                "Schema": "_timescaledb_internal",
                "Relation Name": "compress_hyper_7_4_chunk",
                "Index Cond": "(river_segment_key = $4)",
                "Filter": (
                    "((_ts_meta_max_2 >= '2026-08-01 00:00:00+00'::timestamp with time zone) AND "
                    "(_ts_meta_min_2 <= '2026-08-08 00:00:00+00'::timestamp with time zone))"
                ),
                "Shared Read Blocks": 0,
                "Shared Hit Blocks": 1,
                "Actual Rows": 8,
                "Actual Loops": 1,
            }
        ],
    }
    payload = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Shared Read Blocks": 0,
                "Shared Hit Blocks": 12,
                "Actual Rows": 8,
                "Plans": [initplan, indexed],
            }
        }
    ]
    candidates = ("_hyper_6_2_chunk", "compress_hyper_7_4_chunk")
    result = evaluate_explain_json_plan(
        payload,
        candidate_chunk_names=candidates,
        segment_id=TS_SEGMENT,
        river_network_version_id=RNV,
        window_start=ISSUE,
        window_end=WINDOW_END,
    )
    assert result["decompressed_count"] == 1
    assert result["seq_scan"] is False
    seq_child = {
        "Node Type": "Custom Scan",
        "Custom Plan Provider": "DecompressChunk",
        "Schema": "_timescaledb_internal",
        "Relation Name": "_hyper_6_2_chunk",
        "Filter": (
            "((valid_time >= '2026-08-01 00:00:00+00'::timestamp with time zone) AND "
            "(valid_time <= '2026-08-08 00:00:00+00'::timestamp with time zone))"
        ),
        "Shared Read Blocks": 0,
        "Shared Hit Blocks": 1,
        "Actual Rows": 8,
        "Plans": [
            {
                "Node Type": "Seq Scan",
                "Schema": "_timescaledb_internal",
                "Relation Name": "compress_hyper_7_4_chunk",
                "Filter": "(river_segment_key = $4)",
                "Shared Read Blocks": 0,
                "Shared Hit Blocks": 1,
                "Actual Rows": 8,
            }
        ],
    }
    seq_payload = [
        {
            "Plan": {
                "Node Type": "Nested Loop",
                "Shared Read Blocks": 0,
                "Shared Hit Blocks": 12,
                "Actual Rows": 8,
                "Plans": [initplan, seq_child],
            }
        }
    ]
    _assert_code(
        lambda: evaluate_explain_json_plan(
            seq_payload,
            candidate_chunk_names=candidates,
            segment_id=TS_SEGMENT,
            river_network_version_id=RNV,
            window_start=ISSUE,
            window_end=WINDOW_END,
        ),
        "PLAN_SEQ_SCAN",
    )


def test_root_empty_query_still_refuses_no_rows() -> None:
    payload = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_WINDOW_COND,
        actual_rows=0,
        rows_removed=0,
        root_rows=0,
    )
    _assert_code(lambda: evaluate_explain_json_plan(payload, **_native_kwargs()), "PLAN_NO_ROWS")
    empty = _plan(actual_rows=0)
    _assert_code(
        lambda: evaluate_explain_json_plan(
            empty,
            candidate_chunk_names=("_hyper_1_1_chunk",),
            segment_id=TS_SEGMENT,
        ),
        "PLAN_NO_ROWS",
    )


def test_nonempty_query_allows_empty_in_window_candidate_probe() -> None:
    empty_probe = _native_index_access(
        relation="_hyper_6_4_chunk",
        index_cond=NATIVE_WINDOW_COND,
        actual_rows=0,
        actual_loops=1,
        rows_removed=0,
        shared_hit=2,
    )
    payload = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_WINDOW_COND,
        actual_rows=2,
        actual_loops=1,
        rows_removed=0,
        root_rows=2,
        extra_access=empty_probe,
    )
    result = evaluate_explain_json_plan(
        payload,
        **_native_kwargs(candidate_chunk_names=("_hyper_6_1_chunk", "_hyper_6_4_chunk")),
    )
    assert result["actual_rows"] == 2
    assert result["touched_count"] == 2
    assert result["decompressed_count"] == 0
    assert result["shared_buffer_blocks"] == 26
    assert result["seq_scan"] is False


def test_zero_returned_positive_filter_still_refuses_ratio() -> None:
    payload = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_WINDOW_COND,
        actual_rows=0,
        actual_loops=1,
        rows_removed=11,
        root_rows=2,
    )
    _assert_code(lambda: evaluate_explain_json_plan(payload, **_native_kwargs()), "PLAN_FILTER_RATIO")
    wasted = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_WINDOW_COND,
        extra_access=_native_index_access(
            relation="_hyper_6_4_chunk",
            index_cond=NATIVE_WINDOW_COND,
            actual_rows=0,
            actual_loops=1,
            rows_removed=3,
        ),
    )
    _assert_code(
        lambda: evaluate_explain_json_plan(
            wasted,
            **_native_kwargs(candidate_chunk_names=("_hyper_6_1_chunk", "_hyper_6_4_chunk")),
        ),
        "PLAN_FILTER_RATIO",
    )


def test_missing_root_row_metrics_refuse_without_synthesizing_one() -> None:
    payload = _native_index_plan(
        relation="_hyper_6_1_chunk",
        index_cond=NATIVE_WINDOW_COND,
        root_rows=None,
    )
    payload[0]["Plan"].pop("Actual Rows", None)
    for node in payload[0]["Plan"]["Plans"]:
        node.pop("Actual Rows", None)
    _assert_code(lambda: evaluate_explain_json_plan(payload, **_native_kwargs()), "PLAN_ROWS_INVALID")
    allowed = evaluate_explain_json_plan(payload, **_native_kwargs(), allow_empty_rows=True)
    assert allowed["actual_rows"] == 0
    assert allowed["touched_count"] == 1
