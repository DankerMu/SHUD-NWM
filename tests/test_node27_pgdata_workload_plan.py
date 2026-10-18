"""Native EXPLAIN plan regressions for the PGDATA workload owner.

These cases live beside ``tests/test_node27_pgdata_workload.py`` so each
module stays under the 1,000-line structural limit. Shared constants and
helpers are reused from that suite; this file does not reimplement them.
"""

from __future__ import annotations

import ast
import inspect
import re
import subprocess
from pathlib import Path
from typing import Any

import packages.common.node27_pgdata_workload_plan as plan_module
from packages.common.node27_pgdata_workload_plan import (
    CANDIDATE_CHUNKS_SQL,
    evaluate_explain_json_plan,
    load_candidate_relations,
)
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


REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_TOKEN = "river_timeseries_legacy"
LEGACY_GUARD_ROOTS = ("apps", "packages", "services", "workers", "scripts")
ADVICE_FILE = "packages/common/display_coverage.py"
ADVICE_NAME = "_REFUSAL_ADVICE"
_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _legacy_token_hits(source: str, path: str) -> list[tuple[int, str]]:
    """Classify every str constant naming the dropped table as docstring, advice, or code."""

    tree = ast.parse(source, filename=path)
    docstrings: set[int] = set()
    advice: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_OWNERS) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.add(id(first.value))
        if (
            path == ADVICE_FILE
            and isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == ADVICE_NAME for target in node.targets)
        ):
            advice.update(id(sub) for sub in ast.walk(node.value))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and LEGACY_TOKEN in node.value:
            if id(node) in docstrings:
                category = "docstring"
            elif id(node) in advice:
                category = "advice"
            else:
                category = "code"
            hits.append((node.lineno, category))
    return sorted(hits)


def _repo_legacy_token_hits() -> dict[str, list[str]]:
    listed = subprocess.run(
        ["git", "ls-files", "--", *LEGACY_GUARD_ROOTS],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    by_category: dict[str, list[str]] = {"code": [], "docstring": [], "advice": []}
    for rel in listed:
        path = REPO_ROOT / rel
        if not rel.endswith(".py") or "tests" in Path(rel).parts or not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        if LEGACY_TOKEN not in source:
            continue
        for lineno, category in _legacy_token_hits(source, rel):
            by_category[category].append(f"{rel}:{lineno}")
    return by_category


def _narrow_candidate_row(chunk: str, *, compressed: str | None = None) -> dict[str, Any]:
    return {
        "hypertable_schema": "hydro",
        "hypertable_name": "river_timeseries",
        "chunk_schema": "_timescaledb_internal",
        "chunk_name": chunk,
        "range_start": ISSUE,
        "range_end": WINDOW_END,
        "is_compressed": compressed is not None,
        "compressed_schema": "_timescaledb_internal" if compressed else None,
        "compressed_name": compressed,
    }


def test_narrow_only_catalog_loads_candidates_without_missing_relation() -> None:
    rows = [
        _narrow_candidate_row("_hyper_1_1_chunk"),
        _narrow_candidate_row("_hyper_1_2_chunk", compressed="compress_hyper_1_2_chunk"),
    ]
    seen: list[str] = []

    def execute(sql: str, _params: object = None) -> list[dict[str, Any]]:
        seen.append(sql)
        return rows

    loaded = load_candidate_relations(execute, window_start=ISSUE, window_end=WINDOW_END)
    assert seen == [CANDIDATE_CHUNKS_SQL]
    assert loaded["origin_chunk_names"] == ("_hyper_1_1_chunk", "_hyper_1_2_chunk")
    assert loaded["compressed_chunk_names"] == ("compress_hyper_1_2_chunk",)
    assert loaded["candidate_count"] == 3


def test_default_expected_relations_name_only_the_narrow_parent() -> None:
    assert not hasattr(plan_module, "LEGACY_HYPERTABLE")
    default = inspect.signature(evaluate_explain_json_plan).parameters["expected_relations"].default
    assert default == ("river_timeseries",)
    payload = _native_index_plan(relation="_hyper_6_1_chunk", index_cond=NATIVE_WINDOW_COND)
    result = evaluate_explain_json_plan(payload, **_native_kwargs())
    assert result["expected_relations"] == ["river_timeseries"]


def test_candidate_sql_binds_only_the_narrow_parent() -> None:
    assert LEGACY_TOKEN not in CANDIDATE_CHUNKS_SQL
    assert re.findall(r"\('(\w+)', '(\w+)'\)", CANDIDATE_CHUNKS_SQL) == [("hydro", "river_timeseries")]


def test_legacy_token_classifier_flags_code_and_allows_docstrings_comments_and_advice() -> None:
    assert _legacy_token_hits('X = "hydro.river_timeseries_legacy"\n', "packages/x.py") == [(1, "code")]
    docstrings = (
        '"""Module names hydro.river_timeseries_legacy."""\n'
        "\n"
        "class Owner:\n"
        '    """Class names river_timeseries_legacy."""\n'
        "\n"
        "    def sync(self):\n"
        '        """Function names river_timeseries_legacy."""\n'
        "\n"
        "    async def run(self):\n"
        '        """Coroutine names river_timeseries_legacy."""\n'
        '        "a later bare string river_timeseries_legacy is not a docstring"\n'
    )
    assert _legacy_token_hits(docstrings, "packages/x.py") == [
        (1, "docstring"),
        (4, "docstring"),
        (7, "docstring"),
        (10, "docstring"),
        (11, "code"),
    ]
    comment = "# hydro.river_timeseries_legacy was dropped on 2026-09-19\nY = 1\n"
    assert _legacy_token_hits(comment, "packages/x.py") == []
    advice = '_REFUSAL_ADVICE = (\n    "rows went with "\n    "hydro.river_timeseries_legacy"\n)\n'
    assert _legacy_token_hits(advice, ADVICE_FILE) == [(2, "advice")]
    assert _legacy_token_hits(advice, "packages/common/other.py") == [(2, "code")]


def test_legacy_table_name_only_in_docstrings_comments_or_refusal_advice() -> None:
    hits = _repo_legacy_token_hits()
    assert hits["code"] == [], "river_timeseries_legacy outside docstring/comment/_REFUSAL_ADVICE: " + ", ".join(
        hits["code"]
    )
    assert len(hits["advice"]) == 1, hits["advice"]
    assert hits["advice"][0].startswith(f"{ADVICE_FILE}:")
