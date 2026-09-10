"""#1342 product-curve SQL/API oracle and EXPLAIN JSON contract. No live DB/API."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from packages.common.compressed_chunk_cold_residency import (
    ResidencyGroup,
    ResidencyMember,
)
from packages.common.node27_issue1895_catalog import classify_complete_groups
from packages.common.node27_issue1895_identity import bind_hot_identities
from packages.common.node27_issue1895_lanes import (
    LANE_NAMES,
    freeze_lanes,
    select_cold_identity,
)
from packages.common.node27_issue1895_percentiles import (
    ACCEPTED_SAMPLE_COUNT,
    P95_NEAREST_RANK_INDEX,
    nearest_rank_p95,
)
from packages.common.node27_issue1895_performance import (
    HTTP_BODY_LIMIT_BYTES,
    build_performance_receipt,
    evaluate_api_lane,
    evaluate_named_lane,
    evaluate_sql_lane,
    run_warmup_and_accepted,
    validate_performance_receipt,
)
from packages.common.node27_issue1895_query import (
    record_explicit_cycle_curve,
    timeseries_segment_id,
    validate_river_series_response,
)
from packages.common.node27_issue1895_sql import (
    PLAN_BUFFER_LIMIT,
    PLAN_MAX_ARRAY_ITEMS,
    PLAN_MAX_BYTES,
    PLAN_MAX_DEPTH,
    PLAN_MAX_NODES,
    bound_explain_payload,
    evaluate_explain_json_plan,
    normalize_candidate_chunk_name,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from tests.test_issue1895_runbook_contract import _gate, _gate_bash, _gate_lines

SHA = "a" * 40
SEGMENT = "qhh_reach_000042"
TS_SEGMENT = "qhh_shud_riv_000042"
BASIN = "basins_qhh"
BV = "bv-1"
RNV = "rnv-1"
ISSUE = "2026-08-01T00:00:00Z"
WINDOW_END = "2026-08-08T00:00:00Z"
API_PATH = f"/api/v1/basin-versions/{BV}/river-segments/{SEGMENT}/forecast-series"
RANGE_START = datetime(2026, 8, 1, tzinfo=UTC)
RANGE_END = datetime(2026, 8, 8, tzinfo=UTC)
FORECAST_SQL_MARKERS = (
    "FROM hydro.river_timeseries rt",
    "h.run_type = 'forecast'",
    "h.cycle_time = %(issue_time)s",
)


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
) -> list[dict]:
    nodes: list[dict] = []
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
    if not nodes or decompress:
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
        if decompress and nodes:
            index_node["Plans"] = [nodes[0]]
            current = nodes[0]
            for node in nodes[1:]:
                current["Plans"] = [node]
                current = node
            nodes = [index_node]
        elif not nodes:
            nodes.append(index_node)
    plan: dict = {
        "Node Type": "Limit",
        "Index Cond": index_cond or f"(river_segment_id = '{TS_SEGMENT}')",
        "Shared Read Blocks": buffers,
        "Shared Hit Blocks": hit,
        "Actual Rows": actual_rows,
        "Plans": [nodes[0]],
    }
    current = nodes[0]
    for node in nodes[1:]:
        current["Plans"] = [node]
        current = node
    return [{"Plan": plan}]


def _sql_samples(plan: list[dict], duration: float = 10) -> tuple[dict, list[dict]]:
    warmup = {"index": 0, "discarded": True, "duration_ms": 1, "explain_json": plan}
    accepted = [
        {"index": index, "discarded": False, "duration_ms": duration, "explain_json": plan}
        for index in range(1, 21)
    ]
    return warmup, accepted


def _api_samples(duration: float = 20, status: int = 200, body_len: int = 32) -> tuple[dict, list[dict]]:
    warmup = {"index": 0, "discarded": True, "duration_ms": 5, "status": status, "body_len": 8}
    accepted = [
        {"index": index, "discarded": False, "duration_ms": duration, "status": status, "body_len": body_len}
        for index in range(1, 21)
    ]
    return warmup, accepted


def _lane_query(*, source: str, run_id: str, issue: str = ISSUE) -> dict:
    return record_explicit_cycle_curve(
        basin_version_id=BV,
        segment_id=SEGMENT,
        river_network_version_id=RNV,
        issue_time=issue,
        run_id=run_id,
        model_id="model-1",
        source=source,
    )


def test_nearest_rank_p95_uses_index_18_for_twenty_samples() -> None:
    samples = [float(index) for index in range(20)]
    assert nearest_rank_p95(samples) == 18.0
    assert P95_NEAREST_RANK_INDEX == 18
    with pytest.raises(Issue1895ReadinessError):
        nearest_rank_p95(samples[:-1])


def test_warmup_is_discarded_and_exactly_twenty_accepted() -> None:
    def probe(index: int) -> dict:
        return {"duration_ms": 10 + index, "status": 200, "body_len": 16}

    warmup, accepted = run_warmup_and_accepted(probe)
    assert warmup["discarded"] is True and warmup["index"] == 0
    assert len(accepted) == ACCEPTED_SAMPLE_COUNT
    assert [sample["index"] for sample in accepted] == list(range(1, 21))
    assert all(sample["discarded"] is False for sample in accepted)


def test_explicit_cycle_recorder_binds_run_model_and_reach_mapping() -> None:
    recorded = _lane_query(source="GFS", run_id="run-gfs-hot")
    assert timeseries_segment_id(SEGMENT) == TS_SEGMENT
    assert recorded["timeseries_segment_id"] == TS_SEGMENT
    sql = recorded["sql"]
    for marker in FORECAST_SQL_MARKERS:
        assert marker in sql
    assert "selected_cycles" not in sql
    assert "WITH selected_cycles" not in sql
    parameters = recorded["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["issue_time"] == datetime(2026, 8, 1, tzinfo=UTC)
    assert parameters["run_id"] == recorded["run_id"]
    assert parameters["model_id"] == recorded["model_id"]
    assert parameters["river_segment_id"] == TS_SEGMENT
    assert recorded["api_path"] == API_PATH
    assert "variables=q_down" in recorded["api_query"]
    assert "scenarios=forecast_gfs_deterministic" in recorded["api_query"]
    assert "include_analysis=false" in recorded["api_query"]
    assert f"run_id={recorded['run_id']}" in recorded["api_query"]
    assert f"model_id={recorded['model_id']}" in recorded["api_query"]
    with pytest.raises(Issue1895ReadinessError) as missing:
        record_explicit_cycle_curve(
            basin_version_id=BV,
            segment_id=SEGMENT,
            river_network_version_id=RNV,
            issue_time=ISSUE,
            run_id="",
            model_id="model-1",
            source="GFS",
        )
    assert missing.value.code == "QUERY_IDENTITY_MISSING"


def test_river_series_response_rejects_envelope_and_empty_series() -> None:
    start_ms = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
    ok = {
        "segment_id": SEGMENT,
        "issue_time": ISSUE,
        "unit": "m3/s",
        "series": [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "source_id": "GFS",
                "cycle_time": ISSUE,
                "points": [[start_ms, 2.0]],
            }
        ],
    }
    kwargs = dict(
        segment_id=SEGMENT,
        issue_time=ISSUE,
        scenario="forecast_gfs_deterministic",
        source="GFS",
        window_start=ISSUE,
        window_end=WINDOW_END,
    )
    assert validate_river_series_response(ok, **kwargs)["point_count"] == 1
    with pytest.raises(Issue1895ReadinessError) as envelope:
        validate_river_series_response({"status": "ok", "data": ok}, **kwargs)
    assert envelope.value.code == "API_ENVELOPE_INVALID"
    with pytest.raises(Issue1895ReadinessError) as empty:
        validate_river_series_response(
            {"segment_id": SEGMENT, "issue_time": ISSUE, "unit": "m3/s", "series": []},
            **kwargs,
        )
    assert empty.value.code == "API_SERIES_EMPTY"
    wrong_source = json.loads(json.dumps(ok))
    wrong_source["series"][0]["source_id"] = "IFS"
    with pytest.raises(Issue1895ReadinessError) as source:
        validate_river_series_response(wrong_source, **kwargs)
    assert source.value.code == "API_SOURCE_MISMATCH"
    missing_cycle = json.loads(json.dumps(ok))
    missing_cycle["issue_time"] = None
    missing_cycle["series"][0]["cycle_time"] = "1999-01-01T00:00:00Z"
    with pytest.raises(Issue1895ReadinessError):
        validate_river_series_response(missing_cycle, **kwargs)
    bad_point = json.loads(json.dumps(ok))
    bad_point["series"][0]["points"] = [["not-time", "not-number"]]
    with pytest.raises(Issue1895ReadinessError):
        validate_river_series_response(bad_point, **kwargs)
    oow = json.loads(json.dumps(ok))
    oow["series"][0]["points"] = [[0, 1.0]]
    with pytest.raises(Issue1895ReadinessError) as window:
        validate_river_series_response(oow, **kwargs)
    assert window.value.code == "API_POINT_OUT_OF_WINDOW"
    dup = json.loads(json.dumps(ok))
    dup["series"].append(dup["series"][0])
    with pytest.raises(Issue1895ReadinessError) as ambiguous:
        validate_river_series_response(dup, **kwargs)
    assert ambiguous.value.code == "API_SERIES_AMBIGUOUS"
    plus = json.loads(json.dumps(ok))
    plus["issue_time"] = "2026-08-01T00:00:00+00:00"
    plus["series"][0]["cycle_time"] = "2026-08-01T00:00:00+00:00"
    assert validate_river_series_response(plus, **kwargs)["point_count"] == 1


def _member(*, kind: str, oid: int, name: str, tablespace: str, toast_oid: int | None = None) -> ResidencyMember:
    relkind = "i" if "index" in kind else "r"
    return ResidencyMember(
        kind=kind,  # type: ignore[arg-type]
        oid=oid,
        schema="_timescaledb_internal",
        name=name,
        relkind=relkind,
        tablespace=tablespace,
        bytes=8,
        toast_oid=toast_oid,
        heap_oid=None if "index" not in kind else oid - 1,
    )


def _group(
    *,
    name: str,
    origin_oid: int,
    compressed: bool,
    members: tuple[ResidencyMember, ...],
    compressed_oid: int | None = None,
) -> ResidencyGroup:
    return ResidencyGroup(
        hypertable_schema="hydro",
        hypertable_name="river_timeseries",
        origin_oid=origin_oid,
        origin_schema="_timescaledb_internal",
        origin_name=name,
        compressed_oid=compressed_oid,
        compressed_schema="_timescaledb_internal" if compressed else None,
        compressed_name=f"{name}_c" if compressed else None,
        range_start=RANGE_START,
        range_end=RANGE_END,
        is_compressed=compressed,
        members=members,
    )


def test_complete_groups_reject_origin_only_and_mixed_members() -> None:
    origin_only = _group(
        name="_hyper_1_1_chunk",
        origin_oid=11,
        compressed=True,
        compressed_oid=12,
        members=(_member(kind="origin_heap", oid=11, name="_hyper_1_1_chunk", tablespace="nhms_cold"),),
    )
    with pytest.raises(Issue1895ReadinessError) as origin:
        classify_complete_groups([origin_only], kind="cold")
    assert origin.value.code == "LANE_COLD_NOT_TARGET"
    hot = _group(
        name="_hyper_1_2_chunk",
        origin_oid=21,
        compressed=False,
        members=(
            _member(kind="origin_heap", oid=21, name="_hyper_1_2_chunk", tablespace="pg_default", toast_oid=22),
            _member(kind="toast_heap", oid=22, name="_hyper_1_2_chunk_toast", tablespace="pg_default"),
            _member(kind="toast_index", oid=23, name="_hyper_1_2_chunk_toast_idx", tablespace="pg_default"),
            _member(kind="index", oid=24, name="_hyper_1_2_chunk_idx", tablespace="pg_default"),
        ),
    )
    classified_hot = classify_complete_groups([hot], kind="hot")
    assert classified_hot["state"] == "hot_uncompressed_source"
    cold = _group(
        name="_hyper_1_3_chunk",
        origin_oid=31,
        compressed=True,
        compressed_oid=32,
        members=(
            _member(kind="origin_heap", oid=31, name="_hyper_1_3_chunk", tablespace="nhms_cold"),
            _member(kind="compressed_heap", oid=32, name="_hyper_1_3_chunk_c", tablespace="nhms_cold"),
            _member(kind="index", oid=33, name="_hyper_1_3_chunk_idx", tablespace="nhms_cold"),
        ),
    )
    classified_cold = classify_complete_groups([cold], kind="cold")
    assert classified_cold["state"] == "cold_compressed_target"
    hot_cold_index = _group(
        name="_hyper_1_4_chunk",
        origin_oid=41,
        compressed=False,
        members=(
            _member(kind="origin_heap", oid=41, name="_hyper_1_4_chunk", tablespace="pg_default"),
            _member(kind="index", oid=42, name="_hyper_1_4_chunk_idx", tablespace="nhms_cold"),
        ),
    )
    with pytest.raises(Issue1895ReadinessError) as mixed_hot:
        classify_complete_groups([hot_cold_index], kind="hot")
    assert mixed_hot.value.code in {"LANE_HOT_NOT_SOURCE", "LANE_MIXED"}
    cold_hot_index = _group(
        name="_hyper_1_5_chunk",
        origin_oid=51,
        compressed=True,
        compressed_oid=52,
        members=(
            _member(kind="origin_heap", oid=51, name="_hyper_1_5_chunk", tablespace="nhms_cold"),
            _member(kind="compressed_heap", oid=52, name="_hyper_1_5_chunk_c", tablespace="nhms_cold"),
            _member(kind="index", oid=53, name="_hyper_1_5_chunk_idx", tablespace="pg_default"),
        ),
    )
    with pytest.raises(Issue1895ReadinessError) as mixed_cold:
        classify_complete_groups([cold_hot_index], kind="cold")
    assert mixed_cold.value.code in {"LANE_COLD_NOT_TARGET", "LANE_MIXED"}
    with pytest.raises(Issue1895ReadinessError) as bound:
        select_cold_identity([], hot_run_id="run-hot", source="GFS", bound=32)
    assert bound.value.code == "LANE_COLD_BOUND"


def test_hot_identity_api_must_match_exact_db_row() -> None:
    product = {
        "status": "ok",
        "data": {
            "status": "ready",
            "availability": {"ready": True},
            "run_id": "run-gfs-hot",
            "model_id": "model-1",
            "basin_id": BASIN,
            "basin_version_id": BV,
            "river_network_version_id": RNV,
            "source_id": "GFS",
            "cycle_time": ISSUE,
            "run_status": "published",
        },
    }
    ifs = json.loads(json.dumps(product))
    ifs["data"]["source_id"] = "IFS"
    ifs["data"]["run_id"] = "run-ifs-hot"
    ifs["data"]["cycle_time"] = "2026-08-02T00:00:00Z"
    db = {
        "GFS": [
            {
                "run_id": "run-gfs-hot",
                "model_id": "model-1",
                "basin_id": BASIN,
                "basin_version_id": BV,
                "river_network_version_id": RNV,
                "source_id": "GFS",
                "cycle_time": ISSUE,
                "status": "published",
            }
        ],
        "IFS": [
            {
                "run_id": "run-ifs-hot",
                "model_id": "model-1",
                "basin_id": BASIN,
                "basin_version_id": BV,
                "river_network_version_id": RNV,
                "source_id": "IFS",
                "cycle_time": "2026-08-02T00:00:00Z",
                "status": "published",
            }
        ],
    }
    bound = bind_hot_identities(api_products={"GFS": product, "IFS": ifs}, db_rows=db, basin_id=BASIN)
    assert bound["GFS"]["run_id"] == "run-gfs-hot"
    drifted = json.loads(json.dumps(db))
    drifted["GFS"][0]["run_id"] = "historical-run"
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        bind_hot_identities(api_products={"GFS": product, "IFS": ifs}, db_rows=drifted, basin_id=BASIN)
    assert mismatch.value.code == "IDENTITY_DB_MISMATCH"


def test_sql_lane_allows_full_decompress_and_fails_seq_scan_p95() -> None:
    candidates = ("_hyper_1_1_chunk",)
    ok_plan = _plan(decompress=["_hyper_1_1_chunk"], buffers=9)
    warmup, accepted = _sql_samples(ok_plan)
    sql_ok = evaluate_sql_lane(
        warmup=warmup,
        accepted=accepted,
        candidate_chunk_names=candidates,
        segment_id=TS_SEGMENT,
        window_start=ISSUE,
        window_end=WINDOW_END,
        lane_kind_name="cold",
    )
    assert sql_ok["p95_ms"] == 10
    assert sql_ok["seq_scan"] is False
    assert sql_ok["all_chunk_decompression"] is False
    seq_plan = _plan(seq=True)
    with pytest.raises(Issue1895ReadinessError) as seq:
        evaluate_explain_json_plan(
            seq_plan, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="hot"
        )
    assert seq.value.code == "PLAN_SEQ_SCAN"
    all_plan = _plan(decompress=list(candidates))
    allowed = evaluate_explain_json_plan(
        all_plan, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="cold"
    )
    assert allowed["decompressed_count"] == 1
    _, slow = _sql_samples(ok_plan, duration=400)
    with pytest.raises(Issue1895ReadinessError) as p95:
        evaluate_sql_lane(
            warmup=warmup,
            accepted=slow,
            candidate_chunk_names=candidates,
            segment_id=TS_SEGMENT,
            window_start=ISSUE,
            lane_kind_name="hot",
        )
    assert p95.value.code == "SQL_P95_EXCEEDED"


def test_plan_loops_and_index_cond_not_filter_token() -> None:
    candidates = ("_hyper_1_1_chunk",)
    looped = _plan(
        decompress=["_hyper_1_1_chunk"],
        actual_rows=1,
        actual_loops=100,
        rows_removed=11,
        index_cond=f"(river_segment_id = '{TS_SEGMENT}')",
    )
    with pytest.raises(Issue1895ReadinessError) as ratio:
        evaluate_explain_json_plan(
            looped,
            candidate_chunk_names=candidates,
            segment_id=TS_SEGMENT,
            lane_kind="cold",
        )
    assert ratio.value.code == "PLAN_FILTER_RATIO"
    filter_only = {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "river_timeseries",
            "Filter": f"(river_segment_id = '{TS_SEGMENT}')",
            "Output": [TS_SEGMENT],
            "Shared Read Blocks": 1,
            "Shared Hit Blocks": 0,
            "Actual Rows": 8,
            "Actual Loops": 1,
        }
    }
    with pytest.raises(Issue1895ReadinessError) as unbound:
        evaluate_explain_json_plan(
            filter_only,
            candidate_chunk_names=candidates,
            segment_id=TS_SEGMENT,
            lane_kind="hot",
        )
    assert unbound.value.code == "PLAN_SEGMENT_UNBOUND"
    hot = _plan(index_cond=f"(river_segment_id = '{TS_SEGMENT}')")
    assert evaluate_explain_json_plan(
        hot, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="hot"
    )["seq_scan"] is False
    cold = _plan(decompress=["_hyper_1_1_chunk"], index_cond=f"(river_segment_id = '{TS_SEGMENT}')")
    assert evaluate_explain_json_plan(
        cold, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="cold"
    )["decompressed_count"] == 1


def test_plan_fails_unrelated_chunk_not_foreign_seq_scan() -> None:
    candidates = ("_hyper_1_1_chunk", "_hyper_1_2_chunk")
    unrelated = _plan(decompress=["_hyper_1_1_chunk", "_hyper_9_9_chunk"])
    with pytest.raises(Issue1895ReadinessError) as caught:
        evaluate_explain_json_plan(
            unrelated, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="cold"
        )
    assert caught.value.code == "PLAN_UNRELATED_CHUNK"
    foreign_seq = _plan(decompress=["_hyper_1_1_chunk"], extra_seq_relation="pg_class")
    result = evaluate_explain_json_plan(
        foreign_seq, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="cold"
    )
    assert result["seq_scan"] is False
    empty = _plan(actual_rows=0)
    with pytest.raises(Issue1895ReadinessError) as no_rows:
        evaluate_explain_json_plan(
            empty, candidate_chunk_names=candidates, segment_id=TS_SEGMENT, lane_kind="hot"
        )
    assert no_rows.value.code == "PLAN_NO_ROWS"


def test_plan_guard_does_not_depend_on_shared_read_buffers_text() -> None:
    text_plan = "Shared Read Buffers: 12\nDecompressChunk"
    with pytest.raises(Issue1895ReadinessError):
        evaluate_explain_json_plan(text_plan, candidate_chunk_names=("_hyper_1_1_chunk",))


def test_api_lane_requires_forecast_series_and_fails_p95_oversize() -> None:
    warmup, ok = _api_samples()
    result = evaluate_api_lane(warmup=warmup, accepted=ok, path=API_PATH)
    assert result["p95_ms"] == 20
    _, slow = _api_samples(duration=600)
    with pytest.raises(Issue1895ReadinessError) as p95:
        evaluate_api_lane(warmup=warmup, accepted=slow, path=API_PATH)
    assert p95.value.code == "API_P95_EXCEEDED"
    _, huge = _api_samples(body_len=70_000)
    with pytest.raises(Issue1895ReadinessError) as body:
        evaluate_api_lane(warmup=warmup, accepted=huge, path=API_PATH)
    assert body.value.code == "API_BODY_LIMIT"
    with pytest.raises(Issue1895ReadinessError) as path:
        evaluate_api_lane(warmup=warmup, accepted=ok, path="/api/v1/layers/discharge/valid-times")
    assert path.value.code == "API_PATH_INVALID"


def _four_lanes() -> dict[str, dict]:
    plan = _plan(decompress=["_hyper_1_1_chunk"], buffers=3)
    sql_warmup, sql_accepted = _sql_samples(plan, duration=11)
    api_warmup, api_accepted = _api_samples(duration=12)
    lanes = {}
    for name, source, run_id in (
        ("gfs_hot", "GFS", "run-gfs-hot"),
        ("ifs_hot", "IFS", "run-ifs-hot"),
        ("gfs_cold", "GFS", "run-gfs-cold"),
        ("ifs_cold", "IFS", "run-ifs-cold"),
    ):
        query = _lane_query(source=source, run_id=run_id)
        lanes[name] = evaluate_named_lane(
            name=name,
            sql_warmup=sql_warmup,
            sql_accepted=sql_accepted,
            api_warmup=api_warmup,
            api_accepted=api_accepted,
            candidate_chunk_names=("_hyper_1_1_chunk",),
            segment_id=TS_SEGMENT,
            window_start=query["window_start"],
            window_end=query["window_end"],
            api_path=query["api_path"],
        )
        lanes[name]["identity"] = {
            "run_id": run_id,
            "model_id": "model-1",
            "basin_id": BASIN,
            "basin_version_id": BV,
            "river_network_version_id": RNV,
            "cycle_time": ISSUE,
            "source_id": source,
            "scenario": query["scenario"],
        }
        lanes[name]["query"] = query
        lanes[name]["query_digest"] = query["query_digest"]
        lanes[name]["state"] = "hot_uncompressed_source" if name.endswith("_hot") else "cold_compressed_target"
        lanes[name]["candidate_chunk_names"] = ["_hyper_1_1_chunk"]
        lanes[name]["run_id"] = run_id
        lanes[name]["model_id"] = "model-1"
        lanes[name]["cycle_time"] = ISSUE
        lanes[name]["basin_id"] = BASIN
        lanes[name]["basin_version_id"] = BV
        lanes[name]["river_network_version_id"] = RNV
        lanes[name]["source_id"] = source
        lanes[name]["scenario"] = query["scenario"]
        lanes[name]["segment_id"] = query["segment_id"]
        lanes[name]["timeseries_segment_id"] = query["timeseries_segment_id"]
        lanes[name]["window_start"] = query["window_start"]
        lanes[name]["window_end"] = query["window_end"]
        lanes[name]["api_path"] = query["api_path"]
        lanes[name]["api_query"] = query["api_query"]
    return lanes


def _pass_receipt() -> dict:
    lanes = _four_lanes()
    frozen = freeze_lanes(lanes)
    identity = {
        "head_sha": SHA,
        "reviewed_sha": SHA,
        "api_origin": "http://127.0.0.1:8080",
        "timeout_seconds": 5,
        "body_limit_bytes": 65536,
        "artifact": "nhms-issue1895-performance-oracle",
        "basin_id": BASIN,
        "segment_id": SEGMENT,
        "readonly": {"transaction_read_only": True, "current_user": "nhms_display_ro"},
    }
    public_lanes = {name: _public_lane(lane) for name, lane in lanes.items()}
    return validate_performance_receipt(
        build_performance_receipt(lanes=public_lanes, identity=identity, frozen=frozen, status="PASS")
    )


def _public_lane(lane: dict) -> dict:
    return {
        "name": lane["name"],
        "kind": lane["kind"],
        "source": lane["source"],
        "state": lane["state"],
        "sql": lane["sql"],
        "api": lane["api"],
        "run_id": lane["run_id"],
        "model_id": lane["model_id"],
        "cycle_time": lane["cycle_time"],
        "basin_id": lane["basin_id"],
        "basin_version_id": lane["basin_version_id"],
        "river_network_version_id": lane["river_network_version_id"],
        "source_id": lane["source_id"],
        "scenario": lane["scenario"],
        "segment_id": lane["segment_id"],
        "timeseries_segment_id": lane["timeseries_segment_id"],
        "window_start": lane["window_start"],
        "window_end": lane["window_end"],
        "api_path": lane["api_path"],
        "api_query": lane["api_query"],
        "query_digest": lane["query_digest"],
        "candidate_chunk_names": lane["candidate_chunk_names"],
    }


def test_four_lane_receipt_rejects_failure_seq_scan_count_and_wrong_path() -> None:
    lanes = _four_lanes()
    frozen = freeze_lanes(lanes)
    identity = {
        "head_sha": SHA,
        "reviewed_sha": SHA,
        "api_origin": "http://127.0.0.1:8080",
        "timeout_seconds": 5,
        "body_limit_bytes": 65536,
        "artifact": "nhms-issue1895-performance-oracle",
        "basin_id": BASIN,
        "segment_id": SEGMENT,
        "readonly": {"transaction_read_only": True, "current_user": "nhms_display_ro"},
    }
    public_lanes = {name: _public_lane(lane) for name, lane in lanes.items()}
    document = validate_performance_receipt(
        build_performance_receipt(lanes=public_lanes, identity=identity, frozen=frozen, status="PASS")
    )
    assert document["status"] == "PASS"
    assert set(document["lanes"]) == set(LANE_NAMES)
    assert "postgresql://" not in json.dumps(document)
    with pytest.raises(Issue1895ReadinessError) as failure:
        build_performance_receipt(
            lanes=lanes,
            identity=identity,
            frozen=frozen,
            status="PASS",
            failure={"code": "SQL_P95_EXCEEDED", "stage": "performance"},
        )
    assert failure.value.code == "RECEIPT_STATUS_INVALID"
    broken = json.loads(json.dumps(document))
    broken["lanes"]["gfs_hot"]["sql"]["seq_scan"] = True
    with pytest.raises(Issue1895ReadinessError) as seq:
        validate_performance_receipt(broken)
    assert seq.value.code == "RECEIPT_SEQ_SCAN"
    count = json.loads(json.dumps(document))
    count["lanes"]["gfs_hot"]["sql"]["accepted_count"] = 1
    with pytest.raises(Issue1895ReadinessError) as samples:
        validate_performance_receipt(count)
    assert samples.value.code == "RECEIPT_SAMPLE_COUNT"
    wrong = json.loads(json.dumps(document))
    wrong["lanes"]["gfs_hot"]["api"]["path"] = "/api/v1/layers/discharge/valid-times"
    with pytest.raises(Issue1895ReadinessError) as path:
        validate_performance_receipt(wrong)
    assert path.value.code == "API_PATH_INVALID"
    warmup0 = json.loads(json.dumps(document))
    warmup0["lanes"]["gfs_hot"]["sql"]["warmup_count"] = 0
    with pytest.raises(Issue1895ReadinessError) as warmup:
        validate_performance_receipt(warmup0)
    assert warmup.value.code == "RECEIPT_WARMUP_COUNT"
    durations = json.loads(json.dumps(document))
    durations["lanes"]["gfs_hot"]["sql"]["accepted_durations_ms"] = [11.0]
    with pytest.raises(Issue1895ReadinessError) as duration_count:
        validate_performance_receipt(durations)
    assert duration_count.value.code == "RECEIPT_DURATION_COUNT"
    status500 = json.loads(json.dumps(document))
    status500["lanes"]["gfs_hot"]["api"]["accepted_statuses"][0] = 500
    with pytest.raises(Issue1895ReadinessError) as api_status:
        validate_performance_receipt(status500)
    assert api_status.value.code == "RECEIPT_API_STATUS"
    frozen_run = json.loads(json.dumps(document))
    frozen_run["frozen"]["gfs_hot"]["run_id"] = "drifted"
    with pytest.raises(Issue1895ReadinessError) as frozen_identity:
        validate_performance_receipt(frozen_run)
    assert frozen_identity.value.code == "RECEIPT_FROZEN_IDENTITY"
    browser = json.loads(json.dumps(document))
    browser["browser"]["p95_limit_ms"] = 1999
    with pytest.raises(Issue1895ReadinessError) as browser_drift:
        validate_performance_receipt(browser)
    assert browser_drift.value.code == "RECEIPT_BROWSER_DRIFT"
    lane_state = json.loads(json.dumps(document))
    lane_state["lanes"]["gfs_hot"]["state"] = "cold_compressed_target"
    with pytest.raises(Issue1895ReadinessError) as state:
        validate_performance_receipt(lane_state)
    assert state.value.code == "RECEIPT_LANE_STATE"
    digest = json.loads(json.dumps(document))
    digest["frozen"]["gfs_hot"]["query_digest"] = "0" * 64
    with pytest.raises(Issue1895ReadinessError):
        validate_performance_receipt(digest)
    p95 = json.loads(json.dumps(document))
    p95["lanes"]["gfs_hot"]["sql"]["p95_ms"] = 99.0
    with pytest.raises(Issue1895ReadinessError) as recompute:
        validate_performance_receipt(p95)
    assert recompute.value.code == "RECEIPT_P95_RECOMPUTE"


def test_inf_sample_cannot_pass_because_rank_18_is_finite() -> None:
    samples = [float(index) for index in range(19)] + [float("inf")]
    with pytest.raises(Issue1895ReadinessError) as caught:
        nearest_rank_p95(samples)
    assert caught.value.code == "P95_SAMPLE_INVALID"
    with pytest.raises(Issue1895ReadinessError):
        nearest_rank_p95([float(index) for index in range(19)] + [float("nan")])
    with pytest.raises(Issue1895ReadinessError):
        run_warmup_and_accepted(lambda _index: {"duration_ms": float("inf"), "status": 200, "body_len": 1})


def test_api_body_len_must_be_present_non_negative_and_bounded() -> None:
    ok = [
        {"index": index, "discarded": False, "duration_ms": 20, "status": 200, "body_len": 32}
        for index in range(1, 21)
    ]
    warmup = {"index": 0, "discarded": True, "duration_ms": 5, "status": 200, "body_len": 8}
    missing = [{**ok[0]}, *ok[1:]]
    del missing[0]["body_len"]
    with pytest.raises(Issue1895ReadinessError) as absent:
        evaluate_api_lane(warmup=warmup, accepted=missing, path=API_PATH)
    assert absent.value.code == "API_BODY_MISSING"
    negative = [{**ok[0], "body_len": -1}, *ok[1:]]
    with pytest.raises(Issue1895ReadinessError) as neg:
        evaluate_api_lane(warmup=warmup, accepted=negative, path=API_PATH)
    assert neg.value.code == "API_BODY_INVALID"
    huge = [{**ok[0], "body_len": HTTP_BODY_LIMIT_BYTES + 1}, *ok[1:]]
    with pytest.raises(Issue1895ReadinessError) as over:
        evaluate_api_lane(warmup=warmup, accepted=huge, path=API_PATH)
    assert over.value.code == "API_BODY_LIMIT"
    equal = [{**sample, "body_len": HTTP_BODY_LIMIT_BYTES} for sample in ok]
    result = evaluate_api_lane(warmup=warmup, accepted=equal, path=API_PATH)
    assert result["p95_ms"] == 20


def test_shared_read_blocks_use_root_query_total_not_parent_plus_child() -> None:
    candidates = ("_hyper_1_1_chunk", "_hyper_1_2_chunk")
    cumulative = {
        "Plan": {
            "Node Type": "Limit",
            "Shared Read Blocks": 7,
            "Shared Hit Blocks": 0,
            "Actual Rows": 8,
            "Index Cond": f"(river_segment_id = '{TS_SEGMENT}')",
            "Plans": [
                {
                    "Node Type": "Index Scan",
                    "Relation Name": "river_timeseries",
                    "Shared Read Blocks": 7,
                    "Plans": [
                        {
                            "Node Type": "Custom Scan",
                            "Custom Plan Provider": "DecompressChunk",
                            "Schema": "_timescaledb_internal",
                            "Relation Name": "_hyper_1_1_chunk",
                            "Shared Read Blocks": 7,
                        }
                    ],
                }
            ],
        }
    }
    result = evaluate_explain_json_plan(cumulative, candidate_chunk_names=candidates)
    assert result["shared_read_blocks"] == 7
    assert result["shared_buffer_blocks"] == 7
    assert result["decompressed_count"] == 1
    zero = _plan(decompress=["_hyper_1_1_chunk"], buffers=0, child_buffers=9)
    assert evaluate_explain_json_plan(zero, candidate_chunk_names=candidates)["shared_read_blocks"] == 0
    allowed = _plan(
        decompress=["_hyper_1_1_chunk"],
        buffers=PLAN_BUFFER_LIMIT,
        child_buffers=PLAN_BUFFER_LIMIT,
    )
    allowed_blocks = evaluate_explain_json_plan(allowed, candidate_chunk_names=candidates)
    assert allowed_blocks["shared_buffer_blocks"] == PLAN_BUFFER_LIMIT
    over = _plan(decompress=["_hyper_1_1_chunk"], buffers=PLAN_BUFFER_LIMIT + 1, child_buffers=0)
    with pytest.raises(Issue1895ReadinessError) as exceeded:
        evaluate_explain_json_plan(over, candidate_chunk_names=candidates)
    assert exceeded.value.code == "PLAN_BUFFERS_EXCEEDED"
    missing = {
        "Plan": {
            "Node Type": "Index Scan",
            "Relation Name": "river_timeseries",
            "Plans": [
                {
                    "Node Type": "Custom Scan",
                    "Custom Plan Provider": "DecompressChunk",
                    "Schema": "_timescaledb_internal",
                    "Relation Name": "_hyper_1_1_chunk",
                    "Shared Read Blocks": 12,
                }
            ],
        }
    }
    with pytest.raises(Issue1895ReadinessError) as absent:
        evaluate_explain_json_plan(missing, candidate_chunk_names=candidates)
    assert absent.value.code == "PLAN_BUFFERS_INVALID"


def test_plan_bounds_reject_excessive_bytes_depth_and_nodes() -> None:
    too_large = {"Node Type": "Index Scan", "padding": "x" * (PLAN_MAX_BYTES + 1)}
    with pytest.raises(Issue1895ReadinessError) as size:
        bound_explain_payload(too_large)
    assert size.value.code == "PLAN_JSON_TOO_LARGE"
    nested: dict[str, object] = {"Node Type": "Index Scan"}
    current = nested
    for _index in range(PLAN_MAX_DEPTH + 2):
        child: dict[str, object] = {"Node Type": "Index Scan"}
        current["Plans"] = [child]
        current = child
    with pytest.raises(Issue1895ReadinessError) as depth:
        bound_explain_payload(nested)
    assert depth.value.code == "PLAN_JSON_TOO_COMPLEX"
    wide = {"Node Type": "Index Scan", "Plans": [{"n": index} for index in range(PLAN_MAX_ARRAY_ITEMS + 1)]}
    with pytest.raises(Issue1895ReadinessError) as nodes:
        bound_explain_payload(wide)
    assert nodes.value.code == "PLAN_JSON_TOO_COMPLEX"
    assert PLAN_MAX_NODES >= 1


def test_schema_qualified_candidate_names_are_rejected() -> None:
    with pytest.raises(Issue1895ReadinessError) as caught:
        normalize_candidate_chunk_name("_timescaledb_internal._hyper_1_1_chunk")
    assert caught.value.code == "PLAN_CANDIDATE_NAME_INVALID"


def test_g7_fence_invokes_the_performance_owner_and_keeps_legacy_display_separate() -> None:
    g7 = _gate("G7")
    lines = " ".join(_gate_lines("G7"))
    assert "scripts/node27_issue1895_performance_oracle.py" in g7
    assert '--basin-id "$BASIN_ID"' in lines
    assert '--segment-id "$SEGMENT_ID"' in lines
    assert "--display-env /home/nwm/NWM/infra/env/display.env" in lines
    assert "--window-start" not in lines
    assert "date -ud '-30 days'" not in g7
    assert "PERF_COMMIT" in g7
    assert "scripts/node27_issue1895_performance_bind.py" in g7
    assert "json.load(open(path))" not in g7
    assert "forecast-series" in g7 or "scripts/node27_issue1895_performance_oracle.py" in g7
    for _opening, body in _gate_bash("G7"):
        assert "Shared Read Buffers:" not in body
    assert "test:e2e:live-river-click" in g7
    assert "test:e2e:live-c4-display" in g7
    assert "c4-receipt-binder.mjs" in g7
    assert "test:e2e:live-display" not in g7
    assert "corepack pnpm@10.11.0 --dir \"$REPO_ROOT/apps/frontend\" run test:e2e:live-river-click" in g7
    assert "river-click-receipt-binder.mjs" in g7
    assert "p95_ms < 2000" in g7 or "THRESHOLD_EXCEEDED" in g7 or "strict" in g7.lower()
    assert '--bracket "$PERF_BRACKET"' in lines


def _mutate_all_chunk(doc: dict) -> None:
    doc["lanes"]["gfs_cold"]["sql"]["all_chunk_decompression"] = True


def _mutate_candidate_count(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["candidate_count"] = 999


def _mutate_frozen_basin(doc: dict) -> None:
    doc["frozen"]["gfs_hot"]["basin_version_id"] = "drift"


def _mutate_metric_buffers(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["plan_metrics"][0]["shared_buffer_blocks"] = 999999


def _mutate_metric_rows(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["plan_metrics"][0]["actual_rows"] = 0


def _mutate_metric_read(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["plan_metrics"][0]["shared_read_blocks"] = -1


def _mutate_seq_scan_string(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["seq_scan"] = "false"


def _mutate_seq_scan_none(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["seq_scan"] = None


def _mutate_metric_extra(doc: dict) -> None:
    doc["lanes"]["gfs_hot"]["sql"]["plan_metrics"][0]["extra"] = 1


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (_mutate_all_chunk, "RECEIPT_ALL_CHUNK_DECOMPRESSION"),
        (_mutate_candidate_count, "RECEIPT_CANDIDATE_COUNT"),
        (_mutate_frozen_basin, "RECEIPT_FROZEN_IDENTITY"),
        (_mutate_metric_buffers, "RECEIPT_PLAN_METRIC_BUFFERS"),
        (_mutate_metric_rows, "RECEIPT_PLAN_METRIC_ROWS"),
        (_mutate_metric_read, "RECEIPT_PLAN_METRIC_BUFFERS"),
        (_mutate_seq_scan_string, "RECEIPT_SEQ_SCAN"),
        (_mutate_seq_scan_none, "RECEIPT_SEQ_SCAN"),
        (_mutate_metric_extra, "RECEIPT_PLAN_METRIC_KEYS"),
    ],
)
def test_receipt_validator_rejects_closed_corruptions(mutate, code: str) -> None:
    document = json.loads(json.dumps(_pass_receipt()))
    mutate(document)
    with pytest.raises(Issue1895ReadinessError) as caught:
        validate_performance_receipt(document)
    assert caught.value.code == code
