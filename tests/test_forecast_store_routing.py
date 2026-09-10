"""I2 capture/serialization evidence, not a PostgreSQL selection simulator.

The cursor supplies independently selected rows. SQL assertions own the placement
of selection and routing; public response assertions own serialization only.
Actual mixed-store row execution belongs to I7.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import ForecastStoreError
from packages.common.river_ts_render import PUSHDOWN_AID_MARKER
from tests.river_ts_template_registry import FORECAST_STORE_EXECUTIONS
from tests.test_forecast_api import SqlCaptureForecastStore, _qhh_candidate_row
from tests.test_qhh_latest_fallback_pushdown import _HEADER_ROW, BindingCheckedCursor, _run_fallback
from tests.test_river_ts_text_identity_cleanup import _CaptureCursor

GFS = datetime(2026, 9, 1, 6, tzinfo=UTC)
IFS = datetime(2026, 9, 1, tzinfo=UTC)
IDENTITY = {"basin_version_id": "basin_v1", "segment_id": "seg_001", "river_network_version_id": "rnv_selected"}

# Literal public contract from isolated pre-routing source d27601e8 (recipe:
# .workplans/issue-1981/implementation/capture_response.py). Not regenerated
# from the routed response builder.
A9_RESPONSE = {
    "basin_id": "basins_qhh",
    "model_id": "basins_qhh_shud",
    "basin_version_id": "basins_qhh_vbasins",
    "river_network_version_id": "basins_qhh_rivnet_vbasins",
    "source_id": "GFS",
    "cycle_time": "2026-05-07T00:00:00Z",
    "run_id": "qhh_gfs_2026050700",
    "forcing_version_id": "forc_qhh_gfs_2026050700_basins_qhh_shud",
    "station_count": 386,
    "expected_station_count": 386,
    "segment_count": 1633,
    "expected_segment_count": 1633,
    "status": "ready",
    "run_status": "parsed",
    "valid_time_start": "2026-05-07T00:00:00Z",
    "valid_time_end": "2026-05-14T00:00:00Z",
    "river_valid_time_start": "2026-05-07T00:00:00Z",
    "river_valid_time_end": "2026-05-14T00:00:00Z",
    "forcing_valid_time_start": "2026-05-07T00:00:00Z",
    "forcing_valid_time_end": "2026-05-14T00:00:00Z",
    "available_horizon_hours": 168,
    "expected_horizon_hours": 168,
    "shorter_horizon": False,
    "availability": {"ready": True, "unavailable_reasons": [], "quality_flags": [], "quality_notes": []},
    "quality": {
        "station_sample_count": 12000,
        "river_sample_count": 10000,
        "required_station_variables": ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"],
        "station_variable_coverage": [
            {
                "variable": variable,
                "station_count": 386,
                "sample_count": 1000,
                "unit_count": 1,
                "quality_flag_count": 1,
                "missing_unit_samples": 0,
                "missing_quality_flag_samples": 0,
                "valid_time_start": "2026-05-07T00:00:00Z",
                "valid_time_end": "2026-05-14T00:00:00Z",
            }
            for variable in ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")
        ],
        "candidate_limit": 1,
        "search_limit": 1,
        "context_limit": 10,
        "query_indexes": [
            {
                "table": "hydro.hydro_run",
                "index": "hydro_run_qhh_latest_candidate_idx",
                "status": "covered_by_latest_product_candidate_index",
                "columns": ["LOWER(source_id)", "run_type", "basin_version_id", "cycle_time DESC", "run_id DESC"],
                "predicate": "cycle_time IS NOT NULL AND status IN ('succeeded', 'parsed', 'published')",
            },
            {
                "table": "core.basin_version",
                "index": "basin_version_qhh_latest_lookup_idx",
                "status": "covered_by_latest_product_basin_lookup_index",
                "columns": ["basin_id", "basin_version_id"],
            },
            {
                "table": "hydro.river_timeseries",
                "index": "river_ts_selected_identity_key_valid_time_idx",
                "status": "covered_by_selected_identity_key_valid_time_index",
                "columns": [
                    "run_key",
                    "basin_version_key",
                    "river_network_version_key",
                    "variable_e",
                    "valid_time DESC",
                ],
            },
            {
                "table": "met.forcing_station_timeseries",
                "index": "forcing_station_timeseries_qhh_latest_window_idx",
                "status": "covered_by_latest_product_station_window_index",
                "columns": [
                    "forcing_version_id",
                    "basin_version_id",
                    "LOWER(source_id)",
                    "variable",
                    "valid_time DESC",
                    "station_id",
                ],
            },
            {
                "table": "met.interp_weight",
                "index": "interp_weight_qhh_latest_membership_idx",
                "status": "covered_by_latest_product_station_membership_index",
                "columns": ["model_id", "station_id", "variable", "LOWER(source_id)"],
            },
        ],
    },
}
PROJECTION = "SELECT rt.run_key, rt.river_network_version_key, rt.valid_time, rt.value, rt.unit_e"

# These are the old outer query contracts, not extracted from the new builder.
OUTER_CLAUSES = {
    "latest_issue_time": (
        "SELECT h.cycle_time",
        "WHERE h.cycle_time IS NOT NULL",
        "ORDER BY h.cycle_time DESC LIMIT 1",
    ),
    "per_source_latest_cycles": (
        "MAX(h.cycle_time) AS cycle_time",
        "WHERE h.run_type = 'forecast'",
        "AND h.cycle_time IS NOT NULL",
        "GROUP BY h.scenario_id ORDER BY h.scenario_id",
    ),
    "latest_analysis_issue_time": (
        "SELECT h.end_time",
        "WHERE h.scenario_id = 'analysis_true_field'",
        "AND h.end_time IS NOT NULL",
        "ORDER BY h.end_time DESC, h.created_at DESC LIMIT 1",
    ),
    "analysis_segment_rows": (
        "SELECT DISTINCT ON (rt.valid_time)",
        "WHERE h.scenario_id = 'analysis_true_field'",
        "AND rt.valid_time >= %(start_time)s",
        "AND rt.valid_time < %(end_time)s",
        "ORDER BY rt.valid_time, h.end_time DESC, h.created_at DESC",
    ),
    "forecast_segment_rows_selected_cycles": (
        "WITH selected_cycles(scenario_id, cycle_time) AS ( VALUES "
        "(%(selected_scenario_0)s, %(selected_cycle_0)s::timestamptz)",
        "JOIN selected_cycles sc ON sc.scenario_id = h.scenario_id AND sc.cycle_time = h.cycle_time",
        "WHERE h.run_type = 'forecast'",
        "AND rt.valid_time >= h.cycle_time",
        "AND rt.valid_time <= h.cycle_time + INTERVAL '7 days'",
        "ORDER BY h.scenario_id, rt.valid_time",
    ),
    "forecast_segment_rows": (
        "WHERE h.run_type = 'forecast'",
        "AND h.cycle_time = %(issue_time)s",
        "AND rt.valid_time >= %(issue_time)s",
        "AND rt.valid_time <= %(end_time)s",
        "ORDER BY h.scenario_id, rt.valid_time",
    ),
    "latest_run_type_valid_time": (
        "SELECT MAX(rt.valid_time) AS valid_time",
        "WHERE LOWER(h.run_type::text) = ANY(%(run_types)s)",
    ),
    "run_type_segment_rows": (
        "WHERE LOWER(h.run_type::text) = ANY(%(run_types)s)",
        "AND rt.valid_time >= %(start_time)s",
        "AND rt.valid_time <= %(end_time)s",
        "ORDER BY h.scenario_id, rt.valid_time",
    ),
}


def assert_spanning_route(sql: str, params: Mapping) -> str:
    assert isinstance(params, Mapping)
    assert "%s" not in sql
    assert set(re.findall(r"%\((\w+)\)s", sql)) == set(params)
    assert sql.count("UNION ALL") == 1
    assert sql.count("FROM hydro.river_timeseries_legacy rt") == 1
    assert len(re.findall(r"FROM hydro\.river_timeseries rt\b", sql)) == 1
    assert sql.count(PROJECTION) == 2
    start = sql.index(PROJECTION)
    end = sql.index(") rt", sql.index("UNION ALL"))
    legacy, narrow = sql[start:end].split("UNION ALL")
    for branch, route in ((legacy, "legacy"), (narrow, "narrow")):
        assert f"WHERE h.timeseries_store = '{route}'" in branch
        assert "JOIN hydro.hydro_run h ON h.run_key = rt.run_key" in branch
        for predicate in (
            "rt.basin_version_key = (",
            "WHERE basin_version_id = %(basin_version_id)s",
            "rt.river_segment_key = (",
            "WHERE river_segment_id = %(river_segment_id)s",
            "AND river_network_version_id = %(river_network_version_id)s",
            "rt.river_network_version_key = (",
            "WHERE river_network_version_id = %(river_network_version_id)s",
            "rt.variable_e = 'q_down'::hydro.river_variable",
        ):
            assert predicate in branch
        assert not re.search(r"\b(MAX|DISTINCT|ORDER BY|GROUP BY|LIMIT)\b", branch)
    assert legacy.count(PUSHDOWN_AID_MARKER) == 3
    assert "rt.river_segment_id = %(river_segment_id)s" in legacy
    assert "rt.river_network_version_id = %(river_network_version_id)s" in legacy
    assert "rt.variable = 'q_down'" in legacy
    assert PUSHDOWN_AID_MARKER not in narrow
    assert not re.search(
        r"rt\.(run_id|basin_version_id|river_segment_id|river_network_version_id|variable|unit|quality_flag)\b",
        narrow,
    )
    return " ".join((sql[:start] + " SOURCE_ROWS " + sql[end:]).split())


@pytest.mark.parametrize("owner", tuple(OUTER_CLAUSES))
def test_each_spanning_execution_routes_before_one_outer_selection(owner):
    sql, params = FORECAST_STORE_EXECUTIONS[owner]()
    outer = assert_spanning_route(sql, params)
    for clause in OUTER_CLAUSES[owner]:
        assert outer.count(clause) == 1, (owner, clause)
    assert params["basin_version_id"] == "basin_v1"
    assert params["river_segment_id"] == "seg_001"
    assert params["river_network_version_id"] == "rivnet_v1"
    if owner in {
        "analysis_segment_rows",
        "forecast_segment_rows",
        "forecast_segment_rows_selected_cycles",
        "run_type_segment_rows",
    }:
        assert outer.count("rt.unit_e::text AS unit") == 1
    if owner in {"forecast_segment_rows", "forecast_segment_rows_selected_cycles", "run_type_segment_rows"}:
        assert (
            "JOIN core.river_network_version rnv ON rnv.river_network_version_key = rt.river_network_version_key"
        ) in outer
    if owner == "analysis_segment_rows":
        assert params["start_time"] == datetime(2026, 5, 7, tzinfo=UTC)
        assert params["end_time"] == datetime(2026, 5, 14, tzinfo=UTC)
    if owner == "run_type_segment_rows":
        assert params["run_types"] == ["hindcast"]
        assert params["start_time"] == datetime(2026, 5, 11, tzinfo=UTC)
        assert params["end_time"] == datetime(2026, 5, 14, tzinfo=UTC)
    if owner == "latest_issue_time":
        cursor = _CaptureCursor([[{"cycle_time": GFS}]])
        selected = forecast_store.PsycopgForecastStore("postgresql://unit-test")._latest_issue_time(
            cursor,
            **IDENTITY,
            scenario_filter=forecast_store._scenario_filter(["GFS"]),
        )
        assert selected == datetime(2026, 9, 1, 6, tzinfo=UTC)
        assert len(cursor.executed) == 1
        assert_spanning_route(*cursor.executed[0])


@pytest.mark.parametrize("route", ["legacy", "narrow"])
def test_latest_product_uses_header_route_and_preserves_public_response(route):
    candidate = _qhh_candidate_row()
    candidate["timeseries_store"] = route
    store = SqlCaptureForecastStore([[candidate]])
    response = store.latest_qhh_display_product("GFS")
    legacy = SqlCaptureForecastStore([[_qhh_candidate_row()]])
    assert response == legacy.latest_qhh_display_product("GFS") == A9_RESPONSE
    assert response["run_id"] == "qhh_gfs_2026050700"
    assert response["status"] == "ready"
    assert "timeseries_store" not in repr(response)
    header, header_params = store.cursor.header_executions[0]
    sql, params = next((sql, params) for sql, params in store.cursor.executions if "river_sample_rows AS" in sql)
    assert "h.timeseries_store" in header
    assert "run_id,\n                timeseries_store," in header
    river = sql[sql.index("river_sample_rows AS") : sql.index("river_identity_coverage AS")]
    assert f"cr.timeseries_store = '{route}'" in river
    tables = re.findall(r"FROM (hydro\.river_timeseries(?:_legacy)?) rt", river)
    assert tables == ["hydro.river_timeseries_legacy" if route == "legacy" else "hydro.river_timeseries"]
    assert "UNION ALL" not in river
    assert params["scan_run_id"] == "qhh_gfs_2026050700"
    assert "h.run_id = %(scan_run_id)s" in sql
    assert params == legacy.cursor.executions[0][1]
    assert header_params == legacy.cursor.header_executions[0][1]
    old_sql = legacy.cursor.executions[0][0]
    assert (
        sql[sql.index("station_sample_rows AS") : sql.index("river_sample_rows AS")]
        == old_sql[old_sql.index("station_sample_rows AS") : old_sql.index("river_sample_rows AS")]
    )
    if route == "narrow":
        assert PUSHDOWN_AID_MARKER not in river
        assert "rt.run_id" not in river
    else:
        assert river.count(PUSHDOWN_AID_MARKER) == 3


@pytest.mark.parametrize("route", [None, "other", "missing"])
def test_invalid_header_route_fails_before_heavy_execution(route):
    header = dict(_HEADER_ROW, timeseries_store=route)
    if route == "missing":
        del header["timeseries_store"]
    cursor = BindingCheckedCursor(header_rows=[header])
    with pytest.raises(ForecastStoreError) as error:
        _run_fallback(cursor)
    assert error.value.status_code == 500
    assert error.value.code == "TIMESERIES_STORE_INVALID"
    assert not any("river_sample_rows AS" in sql for sql, _ in cursor.executed)


def _target_rows():
    return [
        [{"basin_version_id": "basin_v1"}],
        [{"river_segment_id": "seg_001", "river_network_version_id": "rnv_selected", "properties_json": {}}],
    ]


def _forecast_rows():
    # Independently selected DB result rows. +169h is excluded by PostgreSQL's
    # per-run predicate, asserted above, not by this boundary double.
    return [
        {
            "scenario_id": "forecast_gfs_deterministic",
            "source_id": "GFS",
            "cycle_time": GFS,
            "run_end_time": GFS + timedelta(days=7),
            "valid_time": GFS + timedelta(days=7),
            "value": 12,
            "unit": "m3/s",
        },
        {
            "scenario_id": "forecast_ifs_deterministic",
            "source_id": "IFS",
            "cycle_time": IFS,
            "run_end_time": IFS + timedelta(days=7),
            "valid_time": IFS + timedelta(days=7),
            "value": 9,
            "unit": "m3/s",
        },
    ]


def test_public_latest_forecast_keeps_independent_cycles_and_exact_payload():
    cycles = [
        {"scenario_id": "forecast_ifs_deterministic", "cycle_time": IFS},
        {"scenario_id": "forecast_gfs_deterministic", "cycle_time": GFS},
    ]
    store = SqlCaptureForecastStore(_target_rows() + [cycles, _forecast_rows()])
    response = store.forecast_series(**IDENTITY, issue_time="latest", variables=["q_down"], scenarios=["GFS", "IFS"])
    assert response == {
        "segment_id": "seg_001",
        "issue_time": "2026-09-01T06:00:00Z",
        "unit": "m3/s",
        "series": [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "segment_role": "future_7_days",
                "variable": "q_down",
                "source_id": "GFS",
                "cycle_time": "2026-09-01T06:00:00Z",
                "available_lead_hours": 168,
                "points": [[1788847200000, 12.0]],
            },
            {
                "scenario_id": "forecast_ifs_deterministic",
                "segment_role": "future_7_days",
                "variable": "q_down",
                "source_id": "IFS",
                "cycle_time": "2026-09-01T00:00:00Z",
                "available_lead_hours": 168,
                "points": [[1788825600000, 9.0]],
            },
        ],
    }
    facts = [(sql, params) for sql, params in store.cursor.executions if "hydro.river_timeseries" in sql]
    assert len(facts) == 2
    for sql, params in facts:
        assert_spanning_route(sql, params)
        assert params["scenario_tokens"] == ["gfs", "ifs"]
        assert params["scenario_ids"] == ["forecast_gfs_deterministic", "forecast_ifs_deterministic", "gfs", "ifs"]
    selected = facts[1][1]
    assert selected["selected_scenario_0"] == "forecast_gfs_deterministic"
    assert selected["selected_cycle_0"] == GFS
    assert selected["selected_scenario_1"] == "forecast_ifs_deterministic"
    assert selected["selected_cycle_1"] == IFS


def test_public_splice_preserves_cross_store_analysis_winner():
    analysis = [
        {
            "scenario_id": "analysis_true_field",
            "source_id": "analysis",
            "valid_time": datetime(2026, 8, 31, 23, tzinfo=UTC),
            "value": 2,
            "unit": "m3/s",
        }
    ]
    store = SqlCaptureForecastStore(_target_rows() + [[], [{"end_time": GFS}], analysis])
    response = store.forecast_series(
        **IDENTITY,
        issue_time="latest",
        variables=["q_down"],
        scenarios=[],
        include_analysis=True,
    )
    assert response == {
        "river_segment_id": "seg_001",
        "issue_time": "2026-09-01T06:00:00Z",
        "unit": "m3/s",
        "variable": "discharge",
        "segments": [
            {
                "scenario": "analysis_true_field",
                "scenario_id": "analysis_true_field",
                "source": "ANALYSIS",
                "segment_role": "past_3_days",
                "data": [{"valid_time": "2026-08-31T23:00:00Z", "value": 2.0}],
            }
        ],
    }
    facts = [(sql, params) for sql, params in store.cursor.executions if "hydro.river_timeseries" in sql]
    assert len(facts) == 3
    for sql, params in facts:
        assert_spanning_route(sql, params)
    assert "SELECT DISTINCT ON (rt.valid_time)" in facts[-1][0]
    assert "ORDER BY rt.valid_time, h.end_time DESC, h.created_at DESC" in facts[-1][0]


def test_equivalent_legacy_only_and_narrow_only_rows_keep_exact_forecast_payload():
    # Independent result snapshots representing the same all-legacy/all-narrow
    # values. Different input ordering makes response sorting observable. This
    # double does not evaluate SQL or pretend to select a physical store.
    legacy_rows = [
        {
            "scenario_id": "forecast_gfs_deterministic",
            "source_id": "GFS",
            "cycle_time": GFS,
            "valid_time": GFS,
            "value": 3,
            "unit": "m3/s",
        },
        {
            "scenario_id": "forecast_gfs_deterministic",
            "source_id": "GFS",
            "cycle_time": GFS,
            "valid_time": datetime(2026, 9, 1, 7, tzinfo=UTC),
            "value": 4,
            "unit": "m3/s",
        },
    ]
    narrow_rows = [
        {
            "scenario_id": "forecast_gfs_deterministic",
            "source_id": "GFS",
            "cycle_time": GFS,
            "valid_time": datetime(2026, 9, 1, 7, tzinfo=UTC),
            "value": 4.0,
            "unit": "m3/s",
        },
        {
            "scenario_id": "forecast_gfs_deterministic",
            "source_id": "GFS",
            "cycle_time": GFS,
            "valid_time": GFS,
            "value": 3.0,
            "unit": "m3/s",
        },
    ]
    expected = {
        "segment_id": "seg_001",
        "issue_time": "2026-09-01T06:00:00Z",
        "unit": "m3/s",
        "series": [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "segment_role": "future_7_days",
                "variable": "q_down",
                "source_id": "GFS",
                "cycle_time": "2026-09-01T06:00:00Z",
                "available_lead_hours": 168,
                "points": [[1788242400000, 3.0], [1788246000000, 4.0]],
            }
        ],
    }
    for result_rows in (legacy_rows, narrow_rows):
        store = SqlCaptureForecastStore(_target_rows() + [result_rows])
        response = store.forecast_series(
            **IDENTITY,
            issue_time="2026-09-01T06:00:00Z",
            variables=["q_down"],
            scenarios=["GFS"],
        )
        assert response == expected
        facts = [(sql, params) for sql, params in store.cursor.executions if "hydro.river_timeseries" in sql]
        assert len(facts) == 1
        outer = assert_spanning_route(*facts[0])
        assert "h.cycle_time = %(issue_time)s" in outer
