"""PGDATA workload owner: named capture, identity, P95, plan, API, and CLI."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from packages.common import node27_pgdata_workload_io as workload_io
from packages.common.node27_pgdata_workload import capture_workload_query, measure_workload
from packages.common.node27_pgdata_workload_http import NoRedirect, open_local_get
from packages.common.node27_pgdata_workload_io import (
    publish_measurement_output,
    read_private_dsn_file,
    validate_origin,
)
from packages.common.node27_pgdata_workload_measure import (
    ACCEPTED_SAMPLE_COUNT,
    P95_NEAREST_RANK_INDEX,
    evaluate_api_samples,
    evaluate_sql_samples,
    execute_explain,
    nearest_rank_p95,
    run_warmup_and_accepted,
)
from packages.common.node27_pgdata_workload_plan import (
    evaluate_explain_json_plan,
    load_candidate_relations,
    normalize_candidate_chunk_name,
)
from packages.common.node27_pgdata_workload_query import (
    query_digest,
    record_explicit_cycle_curve,
    timeseries_segment_id,
    validate_captured_explicit_cycle_query,
    validate_river_series_response,
)
from packages.common.node27_pgdata_workload_types import PgdataWorkloadError
from scripts import node27_pgdata_workload as workload_cli
from scripts.node27_pgdata_workload import main as cli_main

BV = "bv-1"
SEGMENT = "qhh_reach_000042"
TS_SEGMENT = "qhh_shud_riv_000042"
RNV = "rnv-1"
ISSUE = "2026-08-01T00:00:00Z"
WINDOW_END = "2026-08-08T00:00:00Z"
RUN = "run-gfs-hot"
MODEL = "model-1"
API_PATH = f"/api/v1/basin-versions/{BV}/river-segments/{SEGMENT}/forecast-series"
SHA = "a" * 40


class _Cursor:
    def __init__(self, payload: Any | None = None, rows: list[Any] | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.payload = payload if payload is not None else [{"Plan": {"Node Type": "Index Scan"}}]
        self.rows = rows if rows is not None else []
        self.description = (
            ("hypertable_schema",),
            ("hypertable_name",),
            ("chunk_schema",),
            ("chunk_name",),
            ("range_start",),
            ("range_end",),
            ("is_compressed",),
            ("compressed_schema",),
            ("compressed_name",),
        )

    def execute(self, statement: str, parameters: object = None) -> None:
        self.calls.append((str(statement), parameters))

    def fetchone(self) -> Any:
        return {"QUERY PLAN": self.payload}

    def fetchall(self) -> list[Any]:
        return list(self.rows)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor | None = None) -> None:
        self.cursor_instance = cursor or _Cursor()
        self.closed = False
        self.rolled_back = False

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True

    def set_session(self, **_kwargs: Any) -> None:
        return None


def _identity() -> dict[str, Any]:
    return {
        "issue_time": datetime(2026, 8, 1, tzinfo=UTC),
        "run_id": RUN,
        "model_id": MODEL,
        "timeseries_segment_id": TS_SEGMENT,
        "river_network_version_id": RNV,
        "basin_version_id": BV,
        "source": "GFS",
        "scenario": "forecast_gfs_deterministic",
        "window_end": datetime(2026, 8, 8, tzinfo=UTC),
    }


def _named_sql(
    *,
    cycle_key: str = "issue_time",
    run_key: str = "run_id",
    model_key: str = "model_id",
    segment_key: str = "river_segment_id",
    network_key: str = "river_network_version_id",
) -> str:
    return f"""
        SELECT rt.valid_time
        FROM hydro.river_timeseries rt
        JOIN hydro.hydro_run h ON h.run_key = rt.run_key
        WHERE h.run_type = 'forecast'
          AND h.cycle_time = %({cycle_key})s
          AND rt.valid_time >= %(issue_time)s
          AND rt.valid_time <= %(end_time)s
          AND h.run_id = %({run_key})s
          AND h.model_id = %({model_key})s
          AND rt.river_segment_id = %({segment_key})s
          AND rt.river_network_version_id = %({network_key})s
          AND rt.basin_version_key = (
              SELECT basin_version_key FROM core.basin_version
              WHERE basin_version_id = %(basin_version_id)s
          )
          AND (LOWER(h.source_id) = ANY(%(scenario_tokens)s) OR LOWER(h.scenario_id) = ANY(%(scenario_ids)s))
    """


def _named_parameters() -> dict[str, Any]:
    return {
        "model_id": MODEL,
        "end_time": datetime(2026, 8, 8, tzinfo=UTC),
        "river_segment_id": TS_SEGMENT,
        "issue_time": datetime(2026, 8, 1, tzinfo=UTC),
        "run_id": RUN,
        "basin_version_id": BV,
        "river_network_version_id": RNV,
        "scenario_tokens": ["forecast_gfs_deterministic"],
        "scenario_ids": ["forecast_gfs_deterministic"],
    }


def _assert_code(call: object, code: str) -> None:
    with pytest.raises(PgdataWorkloadError) as caught:
        call()  # type: ignore[operator]
    assert caught.value.code == code


def _plan(
    *,
    seq: bool = False,
    decompress: list[str] | None = None,
    buffers: int = 12,
    hit: int = 0,
    actual_rows: int = 8,
    actual_loops: int = 1,
    rows_removed: int = 0,
    relation: str = "river_timeseries",
    extra_seq_relation: str | None = None,
    index_cond: str | None = None,
    child_buffers: int | None = None,
    unrelated: str | None = None,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if seq:
        nodes.append(
            {
                "Node Type": "Seq Scan",
                "Relation Name": relation,
                "Shared Read Blocks": buffers,
                "Shared Hit Blocks": hit,
                "Actual Rows": actual_rows,
                "Actual Loops": actual_loops,
                "Rows Removed by Filter": rows_removed,
            }
        )
    if extra_seq_relation:
        nodes.append(
            {
                "Node Type": "Seq Scan",
                "Relation Name": extra_seq_relation,
                "Shared Read Blocks": 1,
                "Shared Hit Blocks": 0,
                "Actual Rows": 1,
            }
        )
    if unrelated:
        nodes.append(
            {
                "Node Type": "Custom Scan",
                "Custom Plan Provider": "DecompressChunk",
                "Schema": "_timescaledb_internal",
                "Relation Name": unrelated,
                "Index Cond": index_cond or f"(river_segment_id = '{TS_SEGMENT}')",
                "Shared Read Blocks": 1,
                "Shared Hit Blocks": 0,
                "Actual Rows": 1,
                "Actual Loops": 1,
            }
        )
    for name in decompress or []:
        nodes.append(
            {
                "Node Type": "Custom Scan",
                "Custom Plan Provider": "DecompressChunk",
                "Schema": "_timescaledb_internal",
                "Relation Name": name,
                "Index Cond": index_cond or f"(river_segment_id = '{TS_SEGMENT}')",
                "Shared Read Blocks": buffers if child_buffers is None else child_buffers,
                "Shared Hit Blocks": hit,
                "Actual Rows": actual_rows,
                "Actual Loops": actual_loops,
                "Rows Removed by Filter": rows_removed,
            }
        )
    if not nodes or decompress or unrelated:
        index_node = {
            "Node Type": "Index Scan",
            "Relation Name": relation,
            "Index Cond": index_cond or f"(river_segment_id = '{TS_SEGMENT}')",
            "Shared Read Blocks": buffers,
            "Shared Hit Blocks": hit,
            "Actual Rows": actual_rows,
            "Actual Loops": actual_loops,
            "Rows Removed by Filter": rows_removed,
        }
        if nodes:
            index_node["Plans"] = [nodes[0]]
            current = nodes[0]
            for node in nodes[1:]:
                current["Plans"] = [node]
                current = node
            nodes = [index_node]
        else:
            nodes.append(index_node)
    plan: dict[str, Any] = {
        "Node Type": "Limit",
        "Index Cond": index_cond or f"(river_segment_id = '{TS_SEGMENT}')",
        "Shared Read Blocks": buffers,
        "Shared Hit Blocks": hit,
        "Actual Rows": actual_rows,
        "Plans": [nodes[0]],
    }
    return [{"Plan": plan}]


def _sql_samples(plan: list[dict[str, Any]], duration: float = 10) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warmup = {"index": 0, "discarded": True, "duration_ms": 1, "explain_json": plan}
    accepted = [
        {"index": index, "discarded": False, "duration_ms": duration, "explain_json": plan} for index in range(1, 21)
    ]
    return warmup, accepted


def _api_samples(
    duration: float = 20,
    status: int = 200,
    body_len: int = 32,
    digest: str = "d" * 64,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    warmup = {
        "index": 0,
        "discarded": True,
        "duration_ms": 5,
        "status": status,
        "body_len": 8,
        "content_digest": digest,
    }
    accepted = [
        {
            "index": index,
            "discarded": False,
            "duration_ms": duration,
            "status": status,
            "body_len": body_len,
            "content_digest": digest,
        }
        for index in range(1, 21)
    ]
    return warmup, accepted


def _ok_series() -> dict[str, Any]:
    start_ms = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
    return {
        "segment_id": SEGMENT,
        "issue_time": ISSUE,
        "unit": "m3/s",
        "series": [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "source_id": "GFS",
                "cycle_time": ISSUE,
                "variable": "q_down",
                "points": [[start_ms, 2.0]],
            }
        ],
    }


def test_shipping_named_capture_preserves_unique_mapping_and_reaches_explain() -> None:
    recorded = record_explicit_cycle_curve(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=ISSUE,
        run_id=RUN,
        model_id=MODEL,
        source="GFS",
    )
    parameters = recorded["parameters"]
    assert isinstance(parameters, Mapping)
    assert set(parameters) >= {
        "basin_version_id",
        "river_segment_id",
        "river_network_version_id",
        "issue_time",
        "end_time",
        "scenario_tokens",
        "scenario_ids",
        "run_id",
        "model_id",
    }
    assert parameters["issue_time"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert parameters["run_id"] == RUN
    assert parameters["model_id"] == MODEL
    assert parameters["river_segment_id"] == TS_SEGMENT
    assert parameters["scenario_tokens"] == ["forecast_gfs_deterministic"]
    assert parameters["scenario_ids"] == ["forecast_gfs_deterministic"]
    assert timeseries_segment_id(SEGMENT) == TS_SEGMENT
    ifs_recorded = record_explicit_cycle_curve(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=ISSUE,
        run_id=RUN,
        model_id=MODEL,
        source="IFS",
    )
    assert ifs_recorded["parameters"]["scenario_tokens"] == ["forecast_ifs_deterministic"]
    assert ifs_recorded["parameters"]["scenario_ids"] == ["forecast_ifs_deterministic"]
    connection = _Connection()
    payload = execute_explain(connection, sql=recorded["explain_sql"], parameters=parameters)
    assert payload == [{"Plan": {"Node Type": "Index Scan"}}]
    passed = connection.cursor_instance.calls[-1][1]
    assert isinstance(passed, Mapping)
    assert passed == parameters
    assert passed is not parameters


def test_named_validator_accepts_arbitrary_order_and_repeated_issue_name() -> None:
    first = _named_parameters()
    second = {key: first[key] for key in reversed(tuple(first))}
    first_result = validate_captured_explicit_cycle_query(_named_sql(), first, _identity())
    second_result = validate_captured_explicit_cycle_query(_named_sql(), second, _identity())
    assert first_result == second_result == first
    assert first_result is not first
    assert query_digest(sql=_named_sql(), parameters=first) == query_digest(sql=_named_sql(), parameters=second)


def test_named_validator_rejects_missing_extra_mixed_and_wrong_bindings() -> None:
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(
            _named_sql(),
            {key: value for key, value in _named_parameters().items() if key != "model_id"},
            _identity(),
        ),
        "QUERY_BINDING_SHAPE",
    )
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(
            _named_sql(),
            {**_named_parameters(), "extra": "value"},
            _identity(),
        ),
        "QUERY_BINDING_SHAPE",
    )
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(_named_sql(), tuple(_named_parameters().values()), _identity()),
        "QUERY_BINDING_SHAPE",
    )
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(
            _named_sql() + " AND rt.valid_time < %s",
            _named_parameters(),
            _identity(),
        ),
        "QUERY_BINDING_SHAPE",
    )
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(
            "WITH selected_cycles AS (SELECT 1) " + _named_sql(),
            _named_parameters(),
            _identity(),
        ),
        "QUERY_BRANCH_INVALID",
    )
    mutated = dict(_named_parameters())
    mutated["run_id"] = "wrong-run"
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(_named_sql(), mutated, _identity()),
        "QUERY_IDENTITY_UNBOUND",
    )
    mutated = dict(_named_parameters())
    mutated["river_segment_id"] = "wrong-segment"
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(_named_sql(), mutated, _identity()),
        "QUERY_SEGMENT_UNBOUND",
    )
    mutated = dict(_named_parameters())
    mutated["scenario_tokens"] = ["gfs"]
    mutated["scenario_ids"] = ["forecast_gfs_deterministic", "gfs"]
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(_named_sql(), mutated, _identity()),
        "QUERY_IDENTITY_UNBOUND",
    )
    ifs_identity = {**_identity(), "source": "IFS", "scenario": "forecast_ifs_deterministic"}
    ifs_parameters = dict(_named_parameters())
    ifs_parameters["scenario_tokens"] = ["forecast_ifs_deterministic"]
    ifs_parameters["scenario_ids"] = ["forecast_ifs_deterministic"]
    assert validate_captured_explicit_cycle_query(_named_sql(), ifs_parameters, ifs_identity) == ifs_parameters
    mutated = dict(ifs_parameters)
    mutated["scenario_tokens"] = ["forecast_gfs_deterministic"]
    mutated["scenario_ids"] = ["forecast_gfs_deterministic"]
    _assert_code(
        lambda: validate_captured_explicit_cycle_query(_named_sql(), mutated, ifs_identity),
        "QUERY_IDENTITY_UNBOUND",
    )


def test_query_digest_is_order_stable_and_time_canonical() -> None:
    first = {
        "issue_time": datetime(2026, 8, 1, tzinfo=UTC),
        "nested": {"a": ["x", 1], "b": "y"},
        "run_id": RUN,
    }
    second = {
        "run_id": RUN,
        "nested": {"b": "y", "a": ["x", 1]},
        "issue_time": datetime(2026, 8, 1, tzinfo=UTC),
    }
    assert query_digest(sql="SELECT  1", parameters=first) == query_digest(sql="SELECT 1", parameters=second)
    changed = {**first, "issue_time": datetime(2026, 8, 2, tzinfo=UTC)}
    assert query_digest(sql="SELECT 1", parameters=first) != query_digest(sql="SELECT 1", parameters=changed)
    original = {"issue_time": datetime(2026, 8, 1, tzinfo=UTC), "nested": ["before"]}
    digest = query_digest(sql="SELECT 1", parameters=original)
    original["nested"].append("after")  # type: ignore[union-attr]
    assert digest != query_digest(sql="SELECT 1", parameters=original)
    _assert_code(lambda: query_digest(sql="SELECT 1", parameters="not-a-container"), "QUERY_BINDING_INVALID")


def test_warmup_is_discarded_and_p95_uses_sorted_index_18() -> None:
    durations = list(range(20, 0, -1))

    def probe(index: int) -> dict[str, Any]:
        if index == 0:
            return {"duration_ms": 999}
        return {"duration_ms": float(durations[index - 1])}

    warmup, accepted = run_warmup_and_accepted(probe)
    assert warmup["discarded"] is True and warmup["index"] == 0
    assert len(accepted) == ACCEPTED_SAMPLE_COUNT
    assert [sample["index"] for sample in accepted] == list(range(1, 21))
    assert nearest_rank_p95([float(sample["duration_ms"]) for sample in accepted]) == 19.0
    assert P95_NEAREST_RANK_INDEX == 18
    equal = nearest_rank_p95([300.0] * 20)
    assert equal == 300.0
    assert nearest_rank_p95([300.0] * 19 + [300.01]) == 300.0
    _assert_code(lambda: nearest_rank_p95([float(index) for index in range(19)]), "P95_SAMPLE_COUNT")
    _assert_code(lambda: nearest_rank_p95([float(index) for index in range(19)] + [float("nan")]), "P95_SAMPLE_INVALID")



def test_threshold_equality_accepted_and_above_refused() -> None:
    candidates = ("_hyper_1_1_chunk",)
    ok_plan = _plan(decompress=["_hyper_1_1_chunk"], buffers=9)
    warmup, accepted = _sql_samples(ok_plan, duration=300)
    sql_ok = evaluate_sql_samples(
        warmup=warmup,
        accepted=accepted,
        candidate_chunk_names=candidates,
        segment_id=TS_SEGMENT,
        window_start=ISSUE,
        window_end=WINDOW_END,
    )
    assert sql_ok["p95_ms"] == 300
    _, slow = _sql_samples(ok_plan, duration=300.01)
    _assert_code(
        lambda: evaluate_sql_samples(
            warmup=warmup,
            accepted=slow,
            candidate_chunk_names=candidates,
            segment_id=TS_SEGMENT,
            window_start=ISSUE,
            window_end=WINDOW_END,
        ),
        "SQL_P95_EXCEEDED",
    )
    api_warmup, api_ok = _api_samples(duration=500)
    api = evaluate_api_samples(warmup=api_warmup, accepted=api_ok, path=API_PATH)
    assert api["p95_ms"] == 500
    _, api_slow = _api_samples(duration=500.01)
    _assert_code(
        lambda: evaluate_api_samples(warmup=api_warmup, accepted=api_slow, path=API_PATH),
        "API_P95_EXCEEDED",
    )


def test_root_buffers_not_double_counted_and_own_decompress_allowed() -> None:
    candidates = ("_hyper_1_1_chunk", "_hyper_1_2_chunk")
    cumulative = {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "river_timeseries",
            "Index Cond": f"(river_segment_id = '{TS_SEGMENT}')",
            "Shared Read Blocks": 9,
            "Shared Hit Blocks": 1,
            "Actual Rows": 8,
            "Actual Loops": 1,
            "Plans": [
                {
                    "Node Type": "Custom Scan",
                    "Custom Plan Provider": "DecompressChunk",
                    "Schema": "_timescaledb_internal",
                    "Relation Name": "_hyper_1_1_chunk",
                    "Index Cond": f"(river_segment_id = '{TS_SEGMENT}')",
                    "Shared Read Blocks": 9,
                    "Shared Hit Blocks": 1,
                    "Actual Rows": 8,
                    "Actual Loops": 1,
                }
            ],
        }
    }
    result = evaluate_explain_json_plan(
        [cumulative],
        candidate_chunk_names=candidates,
        segment_id=TS_SEGMENT,
        window_start=ISSUE,
        window_end=WINDOW_END,
    )
    assert result["shared_buffer_blocks"] == 10
    assert result["decompressed_count"] == 1
    seq = _plan(seq=True)
    _assert_code(
        lambda: evaluate_explain_json_plan(seq, candidate_chunk_names=candidates, segment_id=TS_SEGMENT),
        "PLAN_SEQ_SCAN",
    )
    unrelated = _plan(decompress=["_hyper_1_1_chunk"], unrelated="_hyper_9_9_chunk")
    _assert_code(
        lambda: evaluate_explain_json_plan(unrelated, candidate_chunk_names=candidates, segment_id=TS_SEGMENT),
        "PLAN_UNRELATED_CHUNK",
    )
    foreign = _plan(decompress=["_hyper_1_1_chunk"], extra_seq_relation="pg_class")
    allowed = evaluate_explain_json_plan(foreign, candidate_chunk_names=candidates, segment_id=TS_SEGMENT)
    assert allowed["seq_scan"] is False


def test_chunk_quals_bind_segment_without_index_cond() -> None:
    payload = [
        {
            "Plan": {
                "Node Type": "Custom Scan",
                "Custom Plan Provider": "DecompressChunk",
                "Schema": "_timescaledb_internal",
                "Relation Name": "_hyper_1_1_chunk",
                "Chunk Quals": f"(river_segment_id = '{TS_SEGMENT}')",
                "Shared Read Blocks": 4,
                "Shared Hit Blocks": 1,
                "Actual Rows": 8,
                "Actual Loops": 1,
            }
        }
    ]
    result = evaluate_explain_json_plan(
        payload,
        candidate_chunk_names=("_hyper_1_1_chunk",),
        segment_id=TS_SEGMENT,
        window_start=ISSUE,
        window_end=WINDOW_END,
    )
    assert result["decompressed_count"] == 1
    assert result["shared_buffer_blocks"] == 5


def test_candidate_loader_binds_narrow_legacy_and_physical_compressed() -> None:
    rows = [
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_1_1_chunk",
            "range_start": ISSUE,
            "range_end": WINDOW_END,
            "is_compressed": False,
            "compressed_schema": None,
            "compressed_name": None,
        },
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries_legacy",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_2_1_chunk",
            "range_start": ISSUE,
            "range_end": WINDOW_END,
            "is_compressed": True,
            "compressed_schema": "_timescaledb_internal",
            "compressed_name": "compress_hyper_2_1_chunk",
        },
        {
            "hypertable_schema": "hydro",
            "hypertable_name": "river_timeseries",
            "chunk_schema": "_timescaledb_internal",
            "chunk_name": "_hyper_1_2_chunk",
            "range_start": WINDOW_END,
            "range_end": "2026-08-15T00:00:00Z",
            "is_compressed": False,
            "compressed_schema": None,
            "compressed_name": None,
        },
    ]

    def execute(_sql: str, _params: object = None) -> list[dict[str, Any]]:
        return rows

    loaded = load_candidate_relations(execute, window_start=ISSUE, window_end=WINDOW_END)
    assert "_hyper_1_1_chunk" in loaded["origin_chunk_names"]
    assert "_hyper_1_2_chunk" in loaded["origin_chunk_names"]
    assert "compress_hyper_2_1_chunk" in loaded["compressed_chunk_names"]
    assert "compress_hyper_2_1_chunk" in loaded["candidate_chunk_names"]
    _assert_code(
        lambda: normalize_candidate_chunk_name("_timescaledb_internal._hyper_1_1_chunk"), "PLAN_CANDIDATE_NAME_INVALID"
    )


    def unrelated(_sql: str, _params: object = None) -> list[dict[str, Any]]:
        bad = dict(rows[0])
        bad["hypertable_name"] = "other"
        return [bad]

    _assert_code(
        lambda: load_candidate_relations(unrelated, window_start=ISSUE, window_end=WINDOW_END),
        "PLAN_UNRELATED_PARENT",
    )


def test_api_wrong_identity_empty_series_and_redirect_refuse() -> None:
    kwargs = dict(
        segment_id=SEGMENT,
        issue_time=ISSUE,
        scenario="forecast_gfs_deterministic",
        source="GFS",
        window_start=ISSUE,
        window_end=WINDOW_END,
    )
    assert validate_river_series_response(_ok_series(), **kwargs)["point_count"] == 1
    _assert_code(
        lambda: validate_river_series_response({"status": "ok", "data": _ok_series()}, **kwargs), "API_ENVELOPE_INVALID"
    )
    empty = {"segment_id": SEGMENT, "issue_time": ISSUE, "unit": "m3/s", "series": []}
    _assert_code(lambda: validate_river_series_response(empty, **kwargs), "API_SERIES_EMPTY")
    wrong = json.loads(json.dumps(_ok_series()))
    wrong["series"][0]["source_id"] = "IFS"
    _assert_code(lambda: validate_river_series_response(wrong, **kwargs), "API_SOURCE_MISMATCH")

    class RedirectOpener:
        def open(self, request: Request, timeout: int = 0) -> Any:
            del request, timeout
            raise HTTPError("http://127.0.0.1:1/", 302, "Found", {}, None)  # type: ignore[arg-type]

    _assert_code(
        lambda: open_local_get(
            url="http://127.0.0.1:18080/api/v1/basin-versions/bv/river-segments/seg/forecast-series",
            opener=RedirectOpener(),
            timeout_seconds=1,
            stage="performance",
        ),
        "API_REDIRECT",
    )
    handler = NoRedirect()
    _assert_code(
        lambda: handler.redirect_request(None, None, 301, "Moved", None, "http://example.test"),
        "API_REDIRECT",
    )
    _assert_code(lambda: validate_origin("http://example.test:8080"), "INPUT_ORIGIN_INVALID")


def test_cli_executes_complete_measurement_not_capture_only(tmp_path: Path) -> None:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    dsn_path = parent / "reader.dsn"
    dsn_path.write_text("host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password=secret", encoding="utf-8")
    os.chmod(dsn_path, 0o600)
    output = parent / "workload.json"
    captured = capture_workload_query(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=ISSUE,
        run_id=RUN,
        model_id=MODEL,
        source="GFS",
    )
    sql_calls: list[int] = []
    api_calls: list[int] = []
    plan = _plan(decompress=["_hyper_1_1_chunk"])

    def sql_probe(index: int) -> dict[str, Any]:
        sql_calls.append(index)
        return {"duration_ms": 10, "explain_json": plan}

    def api_probe(index: int) -> dict[str, Any]:
        api_calls.append(index)
        return {"duration_ms": 20, "status": 200, "body_len": 32, "content_digest": "ab" * 32}

    class IdentityCursor(_Cursor):
        def fetchall(self) -> list[Any]:
            last = self.calls[-1][0] if self.calls else ""
            if "timescaledb_information.chunks" in last:
                return [
                    {
                        "hypertable_schema": "hydro",
                        "hypertable_name": "river_timeseries",
                        "chunk_schema": "_timescaledb_internal",
                        "chunk_name": "_hyper_1_1_chunk",
                        "range_start": ISSUE,
                        "range_end": WINDOW_END,
                        "is_compressed": False,
                        "compressed_schema": None,
                        "compressed_name": None,
                    }
                ]
            if "hydro.hydro_run" in last:
                return [
                    {
                        "run_id": RUN,
                        "model_id": MODEL,
                        "source_id": "GFS",
                        "cycle_time": datetime(2026, 8, 1, tzinfo=UTC),
                        "scenario_id": "forecast_gfs_deterministic",
                    }
                ]
            return []

        def fetchone(self) -> Any:
            last = self.calls[-1][0] if self.calls else ""
            if "transaction_read_only" in last:
                return {"current_setting": "on"}
            if "pg_roles" in last:
                return {"current_user": "nhms_display_ro", "rolsuper": False}
            return super().fetchone()

    connection = _Connection(IdentityCursor())
    argv = [
        "measure",
        "--reader-dsn-file",
        str(dsn_path),
        "--api-origin",
        "http://127.0.0.1:18080",
        "--basin-version-id",
        BV,
        "--river-network-version-id",
        RNV,
        "--segment-id",
        SEGMENT,
        "--issue-time",
        ISSUE,
        "--run-id",
        RUN,
        "--model-id",
        MODEL,
        "--source",
        "GFS",
        "--reviewed-sha",
        SHA,
        "--output",
        str(output),
    ]
    rc = cli_main(argv, connection=connection, sql_probe=sql_probe, api_probe=api_probe)
    assert rc == 0
    assert sql_calls == list(range(21))
    assert api_calls == list(range(21))
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["status"] == "PASS"
    assert document["evidence_kind"] == "isolated"
    assert document["isolated"] is True
    assert document["live"] is False
    assert document["identity"]["query_digest"] == captured["query_digest"]
    assert document["query"]["parameters"]["mapping"]["scenario_tokens"] == {
        "sequence": ["forecast_gfs_deterministic"]
    }
    assert "password" not in output.read_text(encoding="utf-8")


def test_cli_refuses_malformed_identity_and_unsafe_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as usage:
        cli_main(["measure"])
    assert usage.value.code == 2
    captured = capsys.readouterr()
    assert "QUERY_USAGE" in captured.err
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    dsn_path = parent / "reader.dsn"
    dsn_path.write_text(
        "host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password=super-secret", encoding="utf-8"
    )
    os.chmod(dsn_path, 0o600)
    output = parent / "workload.json"
    argv = [
        "measure",
        "--reader-dsn-file",
        str(dsn_path),
        "--api-origin",
        "http://example.test:80",
        "--basin-version-id",
        BV,
        "--river-network-version-id",
        RNV,
        "--segment-id",
        SEGMENT,
        "--issue-time",
        ISSUE,
        "--run-id",
        RUN,
        "--model-id",
        MODEL,
        "--source",
        "GFS",
        "--reviewed-sha",
        SHA,
        "--output",
        str(output),
    ]
    rc = cli_main(argv)
    err = capsys.readouterr().err
    assert rc == 1
    assert "INPUT_ORIGIN_INVALID" in err
    assert "super-secret" not in err
    assert not output.exists()
    publish_measurement_output(output, {"status": "PASS"})
    original = output.read_bytes()
    _assert_code(lambda: publish_measurement_output(output, {"status": "FAIL"}), "OUTPUT_EXISTS")
    assert output.read_bytes() == original
    symlink = parent / "link.dsn"
    symlink.symlink_to(dsn_path)
    with pytest.raises(PgdataWorkloadError) as refused:
        read_private_dsn_file(symlink)
    assert "super-secret" not in str(refused.value)
    assert "password=" not in str(refused.value)


def test_measure_requires_complete_sql_and_api_probes() -> None:
    captured = capture_workload_query(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=ISSUE,
        run_id=RUN,
        model_id=MODEL,
        source="GFS",
    )

    def sql_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 10, "explain_json": _plan()}

    def api_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 20, "status": 200, "body_len": 32, "content_digest": "ab" * 32}

    kwargs = {
        "connection": _Connection(),
        "origin": "http://127.0.0.1:18080",
        "captured": captured,
        "evidence_kind": "isolated",
        "reviewed_sha": SHA,
    }
    _assert_code(lambda: measure_workload(**kwargs, sql_probe=sql_probe), "INJECTION_MIXED")
    _assert_code(lambda: measure_workload(**kwargs, api_probe=api_probe), "INJECTION_MIXED")
    rehearsal_kwargs = {**kwargs, "evidence_kind": "rehearsal"}
    _assert_code(lambda: measure_workload(**rehearsal_kwargs), "INPUT_KIND_INVALID")


class _MeasureCursor(_Cursor):
    """Answer the readonly proof, chunk listing and run identity a CLI run probes."""

    def __init__(self, *, read_only: str = "on", current_user: str = "nhms_display_ro") -> None:
        super().__init__()
        self.read_only = read_only
        self.current_user = current_user

    def fetchall(self) -> list[Any]:
        last = self.calls[-1][0] if self.calls else ""
        if "timescaledb_information.chunks" in last:
            return [
                {
                    "hypertable_schema": "hydro",
                    "hypertable_name": "river_timeseries",
                    "chunk_schema": "_timescaledb_internal",
                    "chunk_name": "_hyper_1_1_chunk",
                    "range_start": ISSUE,
                    "range_end": WINDOW_END,
                    "is_compressed": False,
                    "compressed_schema": None,
                    "compressed_name": None,
                }
            ]
        if "hydro.hydro_run" in last:
            return [
                {
                    "run_id": RUN,
                    "model_id": MODEL,
                    "source_id": "GFS",
                    "cycle_time": datetime(2026, 8, 1, tzinfo=UTC),
                    "scenario_id": "forecast_gfs_deterministic",
                }
            ]
        return []

    def fetchone(self) -> Any:
        last = self.calls[-1][0] if self.calls else ""
        if "transaction_read_only" in last:
            return {"current_setting": self.read_only}
        if "pg_roles" in last:
            return {"current_user": self.current_user, "rolsuper": False}
        return super().fetchone()


def _workload_run_dir(tmp_path: Path) -> Path:
    parent = tmp_path / "run"
    parent.mkdir()
    os.chmod(parent, 0o700)
    return parent


def _workload_dsn_file(parent: Path) -> Path:
    dsn_path = parent / "reader.dsn"
    dsn_path.write_text("host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password=secret", encoding="utf-8")
    os.chmod(dsn_path, 0o600)
    return dsn_path


def _measure_argv(
    *,
    dsn_path: Path,
    output: Path,
    reviewed_sha: str = SHA,
    evidence_kind: str | None = None,
) -> list[str]:
    argv = [
        "measure",
        "--reader-dsn-file",
        str(dsn_path),
        "--api-origin",
        "http://127.0.0.1:18080",
        "--basin-version-id",
        BV,
        "--river-network-version-id",
        RNV,
        "--segment-id",
        SEGMENT,
        "--issue-time",
        ISSUE,
        "--run-id",
        RUN,
        "--model-id",
        MODEL,
        "--source",
        "GFS",
        "--reviewed-sha",
        reviewed_sha,
        "--output",
        str(output),
    ]
    if evidence_kind is not None:
        argv += ["--evidence-kind", evidence_kind]
    return argv


def _measure_probes() -> dict[str, Any]:
    plan = _plan(decompress=["_hyper_1_1_chunk"])

    def sql_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 10, "explain_json": plan}

    def api_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 20, "status": 200, "body_len": 32, "content_digest": "ab" * 32}

    return {"sql_probe": sql_probe, "api_probe": api_probe}


def _run_dir_contents(parent: Path) -> list[str]:
    """Name every surviving entry, so a refused run is caught staging a partial sibling."""

    return sorted(entry.name for entry in parent.iterdir())


def _poison_head_resolver() -> str:
    raise AssertionError("an isolated run must never resolve a repository HEAD")


def _git_in(root: Path, *arguments: str) -> str:
    """Run git against a repository this suite owns, isolated from ambient git state."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "issue-2410 workload fixture",
            "GIT_AUTHOR_EMAIL": "workload@example.invalid",
            "GIT_COMMITTER_NAME": "issue-2410 workload fixture",
            "GIT_COMMITTER_EMAIL": "workload@example.invalid",
        }
    )
    return subprocess.run(
        ["git", *arguments], cwd=root, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()


def _tracked_checkout(tmp_path: Path) -> tuple[Path, str]:
    """Build a checkout with one committed tracked file and return it with its HEAD."""

    root = tmp_path / "checkout"
    root.mkdir()
    _git_in(root, "init", "--quiet")
    (root / "tracked.py").write_text("reviewed workload tooling\n", encoding="utf-8")
    _git_in(root, "add", "tracked.py")
    _git_in(root, "commit", "--quiet", "--message", "reviewed workload tooling")
    return root, _git_in(root, "rev-parse", "HEAD")


def test_cli_default_evidence_kind_stays_isolated_and_resolves_no_head(tmp_path: Path) -> None:
    parent = _workload_run_dir(tmp_path)
    output = parent / "workload.json"
    rc = cli_main(
        _measure_argv(dsn_path=_workload_dsn_file(parent), output=output),
        connection=_Connection(_MeasureCursor()),
        head_resolver=_poison_head_resolver,
        **_measure_probes(),
    )
    assert rc == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["evidence_kind"] == "isolated"
    assert document["isolated"] is True
    assert document["live"] is False


def test_cli_live_receipt_differs_from_isolated_only_in_kind_and_timestamp(tmp_path: Path) -> None:
    parent = _workload_run_dir(tmp_path)
    dsn_path = _workload_dsn_file(parent)
    isolated_output = parent / "isolated.json"
    live_output = parent / "live.json"
    isolated_rc = cli_main(
        _measure_argv(dsn_path=dsn_path, output=isolated_output),
        connection=_Connection(_MeasureCursor()),
        **_measure_probes(),
    )
    live_rc = cli_main(
        _measure_argv(dsn_path=dsn_path, output=live_output, evidence_kind="live"),
        connection=_Connection(_MeasureCursor()),
        head_resolver=lambda: SHA,
        **_measure_probes(),
    )
    assert (isolated_rc, live_rc) == (0, 0)
    isolated_document = json.loads(isolated_output.read_text(encoding="utf-8"))
    live_document = json.loads(live_output.read_text(encoding="utf-8"))
    assert live_document["evidence_kind"] == "live"
    assert live_document["isolated"] is False
    assert live_document["live"] is True
    volatile = {"evidence_kind", "isolated", "live", "measured_at"}
    assert {key: value for key, value in live_document.items() if key not in volatile} == {
        key: value for key, value in isolated_document.items() if key not in volatile
    }
    assert live_document["reviewed_sha"] == SHA


@pytest.mark.parametrize(
    ("session", "code"),
    [({"read_only": "off"}, "SQL_NOT_READONLY"), ({"current_user": "postgres"}, "DSN_ROLE_INVALID")],
)
def test_cli_live_refuses_a_session_that_is_not_the_proven_readonly_role(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], session: dict[str, str], code: str
) -> None:
    parent = _workload_run_dir(tmp_path)
    output = parent / "workload.json"
    rc = cli_main(
        _measure_argv(dsn_path=_workload_dsn_file(parent), output=output, evidence_kind="live"),
        connection=_Connection(_MeasureCursor(**session)),
        head_resolver=lambda: SHA,
        **_measure_probes(),
    )
    assert rc == 1
    assert code in capsys.readouterr().err
    assert _run_dir_contents(parent) == ["reader.dsn"]


def test_cli_live_refuses_a_reviewed_sha_that_is_not_the_executing_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = _workload_run_dir(tmp_path)
    output = parent / "workload.json"
    cursor = _MeasureCursor()
    rc = cli_main(
        _measure_argv(dsn_path=_workload_dsn_file(parent), output=output, evidence_kind="live"),
        connection=_Connection(cursor),
        head_resolver=lambda: "b" * 40,
        **_measure_probes(),
    )
    assert rc == 1
    assert "INPUT_SHA_UNBOUND" in capsys.readouterr().err
    assert _run_dir_contents(parent) == ["reader.dsn"]
    # The refusal must land before measuring, not merely before publishing: an
    # unadmitted live run may not touch the measured session at all.
    assert cursor.calls == []


def test_cli_live_refuses_workload_modules_outside_the_executing_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # node-27 runs published script bytes from ~/.local/state/issue1987-tools/<commit>/
    # with PYTHONPATH pointed at the working checkout (runbook 4.10.6). The anchor then
    # owns the script but not the modules that capture, measure and publish, so binding
    # the anchor's HEAD would attribute the samples to a checkout that did not produce
    # them.
    published = tmp_path / "issue1987-tools" / ("c" * 40)
    published.mkdir(parents=True)
    monkeypatch.setattr(workload_cli, "REPO_ROOT", published)
    parent = _workload_run_dir(tmp_path)
    dsn_path = _workload_dsn_file(parent)
    cursor = _MeasureCursor()
    admitting_rc = cli_main(
        _measure_argv(dsn_path=dsn_path, output=parent / "workload.json", evidence_kind="live"),
        connection=_Connection(cursor),
        head_resolver=lambda: SHA,
        **_measure_probes(),
    )
    assert admitting_rc == 1
    assert "INPUT_RUNTIME_UNBOUND" in capsys.readouterr().err
    assert cursor.calls == []
    # The anchor check precedes HEAD resolution: a resolver that must never run does not.
    poisoned_rc = cli_main(
        _measure_argv(dsn_path=dsn_path, output=parent / "poisoned.json", evidence_kind="live"),
        connection=_Connection(_MeasureCursor()),
        head_resolver=_poison_head_resolver,
        **_measure_probes(),
    )
    assert poisoned_rc == 1
    assert "INPUT_RUNTIME_UNBOUND" in capsys.readouterr().err
    assert _run_dir_contents(parent) == ["reader.dsn"]


def test_cli_live_refuses_a_dirty_tracked_checkout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, head = _tracked_checkout(tmp_path)
    (root / "tracked.py").write_text("modified after the reviewed commit\n", encoding="utf-8")
    parent = _workload_run_dir(tmp_path)
    output = parent / "workload.json"
    rc = cli_main(
        _measure_argv(dsn_path=_workload_dsn_file(parent), output=output, reviewed_sha=head, evidence_kind="live"),
        connection=_Connection(_MeasureCursor()),
        head_resolver=lambda: workload_io.resolve_repository_head(root),
        **_measure_probes(),
    )
    assert rc == 1
    assert "INPUT_SHA_UNBOUND" in capsys.readouterr().err
    assert _run_dir_contents(parent) == ["reader.dsn"]


def test_cli_live_refuses_when_the_executing_head_cannot_be_determined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def unavailable() -> str:
        raise OSError("git is unavailable")

    parent = _workload_run_dir(tmp_path)
    output = parent / "workload.json"
    rc = cli_main(
        _measure_argv(dsn_path=_workload_dsn_file(parent), output=output, evidence_kind="live"),
        connection=_Connection(_MeasureCursor()),
        head_resolver=unavailable,
        **_measure_probes(),
    )
    assert rc == 1
    assert "INPUT_SHA_HEAD_UNAVAILABLE" in capsys.readouterr().err
    assert _run_dir_contents(parent) == ["reader.dsn"]


def test_default_head_resolver_is_anchored_at_the_executing_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The seam exists only so tests can repoint it. If the default ever drifts to
    # the process cwd, a live run would bind its receipt to whatever repository the
    # operator happened to stand in.
    checkout = Path(__file__).resolve().parents[1]
    recorded: list[Path] = []

    def recorder(repo_root: Path) -> str:
        recorded.append(repo_root)
        return SHA

    monkeypatch.setattr(workload_cli, "resolve_repository_head", recorder)
    parent = _workload_run_dir(tmp_path)
    dsn_path = _workload_dsn_file(parent)
    output = parent / "workload.json"
    monkeypatch.chdir(tmp_path)
    rc = cli_main(
        _measure_argv(dsn_path=dsn_path, output=output, evidence_kind="live"),
        connection=_Connection(_MeasureCursor()),
        **_measure_probes(),
    )
    assert rc == 0
    assert workload_cli.REPO_ROOT == checkout
    assert recorded == [checkout]
    assert recorded[0] != Path.cwd()
    assert (recorded[0] / "scripts" / "node27_pgdata_workload.py").exists()


def test_cli_rejects_an_unknown_evidence_kind_without_echoing_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dsn_path = tmp_path / "reader.dsn"
    output = tmp_path / "workload.json"
    admitted = workload_cli.build_parser().parse_args(
        _measure_argv(dsn_path=dsn_path, output=output, evidence_kind="live")
    )
    assert admitted.evidence_kind == "live"
    with pytest.raises(SystemExit) as usage:
        cli_main(_measure_argv(dsn_path=dsn_path, output=output, evidence_kind="rehearsal"))
    assert usage.value.code == 2
    err = capsys.readouterr().err
    assert "QUERY_USAGE" in err
    assert "rehearsal" not in err
    assert not output.exists()
