"""I2 capture/serialization evidence, not a PostgreSQL selection simulator.

The cursor supplies independently selected rows. SQL assertions own the placement
of selection; public response assertions own serialization only.

The module was named for ROUTING, and routing is what #1342's contract (task
6.3) deleted: there is one physical river fact table, no
``hydro.hydro_run.timeseries_store`` to read, no ``UNION ALL`` spanning two
stores and no transitional pushdown aids. What survives here is the half that
was never about the store — that every forecast read lands on the SAME single
narrow fact read, with the #2451 sargability spelling and the #2417 run-identity
push intact, and that the public payload is unchanged by any of it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

from packages.common import forecast_store
from packages.common.forecast_store import ForecastStoreError
from tests.river_ts_template_registry import AID_MARKER_TAG, FORECAST_STORE_EXECUTIONS
from tests.test_forecast_api import SqlCaptureForecastStore, _qhh_candidate_row
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


def assert_narrow_fact_read(sql: str, params: Mapping) -> str:
    """One fact read, on one table, with no store predicate and no aid left.

    Before #1342's contract (task 6.3) this helper asserted a two-branch ``UNION
    ALL`` whose branches were told apart by ``h.timeseries_store``, and counted
    the marker-guarded text aids in the legacy branch. The contract deleted the
    second branch, the routing column and the aids, so the assertions that
    remain are the ones that were never about the store: the projection appears
    ONCE, the sub-query reads ``hydro.river_timeseries`` and nothing else, the
    #2451 sargability spelling is intact, and no fact-table text identity column
    is predicated on anywhere.
    """
    assert isinstance(params, Mapping)
    assert "%s" not in sql
    assert set(re.findall(r"%\((\w+)\)s", sql)) == set(params)
    assert "UNION ALL" not in sql
    assert "hydro.river_timeseries_legacy" not in sql
    assert "timeseries_store" not in sql
    assert len(re.findall(r"FROM hydro\.river_timeseries rt\b", sql)) == 1
    assert sql.count(PROJECTION) == 1
    start = sql.index(PROJECTION)
    end = sql.index(") rt", start)
    for branch in (sql[start:end],):
        assert "JOIN hydro.hydro_run h ON h.run_key = rt.run_key" in branch
        for predicate in (
            # #2451 C1: the two redundant identity conjuncts are spelled
            # `IS NOT NULL AND … IS NOT DISTINCT FROM` so they cannot form an
            # index condition on `river_ts_run_discovery_key_idx`'s 2nd and 3rd
            # columns. The guard is the half that keeps this the SAME predicate,
            # enforced in the same place: `IS NOT DISTINCT FROM` alone is TRUE
            # when BOTH sides are NULL, where `=` is UNKNOWN. The nullable side
            # was `hydro.river_timeseries_legacy`
            # (`db/migrations/000050_river_identity_normalization.sql:216-222`)
            # and #1342's contract (task 6.3) dropped it; the guard is KEPT
            # because it is what makes the conjunct non-sargable, which is the
            # behaviour #2451 bought. A pin update, not a behaviour change — the
            # parameter-set assertion at the top of this helper and every
            # row-level case in this module are untouched by it.
            "rt.basin_version_key IS NOT NULL",
            "rt.basin_version_key IS NOT DISTINCT FROM (",
            "WHERE basin_version_id = %(basin_version_id)s",
            # `river_segment_key` keeps its `=`: it is the conjunct that MUST
            # stay sargable, which is what #2451 exists to protect.
            "rt.river_segment_key = (",
            "WHERE river_segment_id = %(river_segment_id)s",
            "AND river_network_version_id = %(river_network_version_id)s",
            "rt.river_network_version_key IS NOT NULL",
            "rt.river_network_version_key IS NOT DISTINCT FROM (",
            "WHERE river_network_version_id = %(river_network_version_id)s",
            "rt.variable_e = 'q_down'::hydro.river_variable",
        ):
            assert predicate in branch
        # `DISTINCT` is listed to forbid `SELECT DISTINCT` inside a branch, which
        # would change row multiplicity under the outer layer. `IS NOT DISTINCT
        # FROM` is a scalar comparison operator and is not that, so the lookbehind
        # excludes it — `DISTINCT` itself is NOT dropped from the alternation.
        assert not re.search(r"\b(MAX|(?<!NOT )DISTINCT|ORDER BY|GROUP BY|LIMIT)\b", branch)
        assert AID_MARKER_TAG not in branch
        assert not re.search(
            r"rt\.(run_id|basin_version_id|river_segment_id|river_network_version_id|variable|unit|quality_flag)\b",
            branch,
        )
    return " ".join((sql[:start] + " SOURCE_ROWS " + sql[end:]).split())


@pytest.mark.parametrize("owner", tuple(OUTER_CLAUSES))
def test_each_registered_execution_reads_one_fact_table_before_one_outer_selection(owner):
    sql, params = FORECAST_STORE_EXECUTIONS[owner]()
    outer = assert_narrow_fact_read(sql, params)
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
        assert_narrow_fact_read(*cursor.executed[0])


def test_latest_product_reads_one_narrow_river_cte_and_preserves_public_response():
    """The retired routing half of A9, re-pinned as its narrow remainder.

    This used to be parametrised over ``h.timeseries_store`` and asserted the
    header carried the routing column and the river CTE was rewritten per store.
    #1342's contract (task 6.3) deleted the column, so what is pinned is that
    the header does NOT select it, the river CTE reads the canonical table once
    with no store predicate and no transitional aid, and the public response is
    byte-for-byte the pre-routing A9 contract above.
    """
    store = SqlCaptureForecastStore([[_qhh_candidate_row()]])

    response = store.latest_qhh_display_product("GFS")

    assert response == A9_RESPONSE
    assert response["run_id"] == "qhh_gfs_2026050700"
    assert response["status"] == "ready"
    assert "timeseries_store" not in repr(response)
    header, _header_params = store.cursor.header_executions[0]
    assert "timeseries_store" not in header
    sql, params = next((sql, params) for sql, params in store.cursor.executions if "river_sample_rows AS" in sql)
    river = sql[sql.index("river_sample_rows AS") : sql.index("river_identity_coverage AS")]
    assert re.findall(r"FROM (hydro\.river_timeseries(?:_legacy)?) rt", river) == ["hydro.river_timeseries"]
    assert "UNION ALL" not in river
    assert "timeseries_store" not in river
    assert AID_MARKER_TAG not in river
    assert "rt.run_id" not in river
    assert params["scan_run_id"] == "qhh_gfs_2026050700"
    assert "h.run_id = %(scan_run_id)s" in sql


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


RESOLVED_RUNS = [
    {"run_key": 101, "run_id": "run_gfs_2026090106"},
    {"run_key": 202, "run_id": "run_ifs_2026090100"},
]


def test_public_latest_forecast_keeps_independent_cycles_and_exact_payload():
    cycles = [
        {"scenario_id": "forecast_ifs_deterministic", "cycle_time": IFS},
        {"scenario_id": "forecast_gfs_deterministic", "cycle_time": GFS},
    ]
    store = SqlCaptureForecastStore(_target_rows() + [cycles, RESOLVED_RUNS, _forecast_rows()])
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
    # The cycle discovery read spans runs by design and pushes nothing (#2424);
    # the fact read it feeds is the one that converges on a resolved run set.
    # Both are the same single narrow read now that routing is gone.
    for sql, params in facts:
        assert_narrow_fact_read(sql, params)
        assert params["scenario_tokens"] == ["gfs", "ifs"]
        assert params["scenario_ids"] == ["forecast_gfs_deterministic", "forecast_ifs_deterministic", "gfs", "ifs"]
    selected = facts[1][1]
    assert selected["selected_scenario_0"] == "forecast_gfs_deterministic"
    assert selected["selected_cycle_0"] == GFS
    assert selected["selected_scenario_1"] == "forecast_ifs_deterministic"
    assert selected["selected_cycle_1"] == IFS
    # #2417 task 3.2: two scenarios on two DISTINCT cycles. The window pushed into
    # each UNION branch must be the ENVELOPE over both — the outer window at
    # `rt.valid_time >= h.cycle_time` is per run and narrows it back. One
    # scenario's cycle used as both bounds would delete the other's rows, and no
    # row digest over a single-scenario measurement could see that.
    assert selected["pushdown_window_start"] == IFS
    assert selected["pushdown_window_end"] == GFS + timedelta(days=7)
    assert selected["pushdown_run_keys"] == [101, 202]
    # #1342's contract (task 6.3) deleted the text twin of this push with the
    # column it predicated on; an unbound placeholder would raise at execute().
    assert "pushdown_run_ids" not in selected
    # Both scenarios survive the push, at the public response boundary.
    assert {series["scenario_id"] for series in response["series"]} == {
        "forecast_gfs_deterministic",
        "forecast_ifs_deterministic",
    }
    resolve = [
        (sql, params) for sql, params in store.cursor.executions if "FROM hydro.hydro_run h" in sql and "rt" not in sql
    ]
    assert len(resolve) == 1
    assert resolve[0][1]["resolve_cycle_times"] == [IFS, GFS]
    assert resolve[0][1]["resolve_scenario_ids"] == [
        "forecast_gfs_deterministic",
        "forecast_ifs_deterministic",
    ]


def _fact_reads(store):
    return [(sql, params) for sql, params in store.cursor.executions if "FROM hydro.river_timeseries rt" in sql]


def _resolve_reads(store):
    return [
        (sql, params)
        for sql, params in store.cursor.executions
        if "FROM hydro.hydro_run h" in sql and "FROM hydro.river_timeseries rt" not in sql
    ]


def test_run_identity_is_resolved_once_in_forecast_series_and_never_for_a_bound_run():
    """#2417 tasks 2.1/2.2: one resolve per request, and none when run_id is bound.

    The bound shape is the D11 capture path — it has no database at capture time
    — so its push has to be the in-SQL scalar sub-select. The unbound shape is
    the live frontend one (`apps/frontend/src/stores/forecast.ts` never sends
    `run_id`) and it resolves.
    """
    bound = SqlCaptureForecastStore(_target_rows() + [_forecast_rows()[:1]])
    bound.forecast_series(
        **IDENTITY,
        issue_time="2026-09-01T06:00:00Z",
        variables=["q_down"],
        scenarios=["GFS"],
        run_id="run_gfs_2026090106",
        model_id="model_a",
    )
    assert _resolve_reads(bound) == []
    facts = _fact_reads(bound)
    assert len(facts) == 1
    assert "AND rt.run_key = (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s)" in facts[0][0]
    assert "pushdown_run_keys" not in facts[0][0]

    unbound = SqlCaptureForecastStore(_target_rows() + [RESOLVED_RUNS[:1], _forecast_rows()[:1]])
    unbound.forecast_series(
        **IDENTITY,
        issue_time="2026-09-01T06:00:00Z",
        variables=["q_down"],
        scenarios=["GFS"],
        model_id="model_a",
    )
    resolves = _resolve_reads(unbound)
    assert len(resolves) == 1
    resolve_sql, resolve_params = resolves[0]
    # Subset discipline: only predicates the outer layer also applies.
    assert "h.run_type = 'forecast'" in resolve_sql
    assert "h.cycle_time = ANY(%(resolve_cycle_times)s)" in resolve_sql
    assert "h.model_id = %(model_id)s" in resolve_sql
    # #2417 fix pass 1: the resolved pair is compared for EQUALITY across the
    # compression window by the node-27 benchmark, so heap order would reject a
    # collection round as `benchmark query identity drift`.
    assert "ORDER BY h.run_key" in resolve_sql
    for absent in ("basin_version_id", "h.status", "timeseries_store"):
        assert absent not in resolve_sql
    assert set(re.findall(r"%\((\w+)\)s", resolve_sql)) <= set(resolve_params)


def test_an_empty_run_resolution_still_raises_the_explicit_cycle_404():
    """#2417 task 3.4: converging run identity must not become a short circuit.

    Returning early on an empty resolve would turn today's 404 RUN_NOT_PUBLISHED
    into a 200 with an empty series. The empty key set is pushed instead — an
    empty ``ANY`` array selects nothing, which is exactly what the unpushed query
    returns when no run matches — and the existing flow produces the same answer.
    """
    store = SqlCaptureForecastStore(_target_rows() + [[], []])

    with pytest.raises(ForecastStoreError) as error:
        store.forecast_series(
            **IDENTITY,
            issue_time="2026-09-01T06:00:00Z",
            variables=["q_down"],
            scenarios=["GFS"],
        )

    assert error.value.status_code == 404
    assert error.value.code == "RUN_NOT_PUBLISHED"
    facts = _fact_reads(store)
    assert len(facts) == 1, "the fact read must still be issued, not skipped"
    assert facts[0][1]["pushdown_run_keys"] == []
    assert "pushdown_run_ids" not in facts[0][1]


def test_an_empty_run_resolution_keeps_the_latest_shape_at_an_empty_200():
    store = SqlCaptureForecastStore(_target_rows() + [[{"scenario_id": "forecast_gfs_deterministic",
                                                        "cycle_time": GFS}], [], []])

    response = store.forecast_series(**IDENTITY, issue_time="latest", variables=["q_down"], scenarios=["GFS"])

    assert response["series"] == []
    assert response["issue_time"] == "2026-09-01T06:00:00Z"
    # cycle discovery + the pushed fact read; the read is not skipped.
    assert len(_fact_reads(store)) == 2


def test_an_empty_run_resolution_still_splices_the_analysis_curve():
    """The dangerous half: a short circuit would delete the analysis series too."""
    analysis = [
        {
            "scenario_id": "analysis_true_field",
            "source_id": "analysis",
            "valid_time": datetime(2026, 8, 31, 23, tzinfo=UTC),
            "value": 2,
            "unit": "m3/s",
        }
    ]
    store = SqlCaptureForecastStore(
        _target_rows()
        + [
            [{"scenario_id": "forecast_gfs_deterministic", "cycle_time": GFS}],
            [],
            analysis,
            [],
        ]
    )

    response = store.forecast_series(
        **IDENTITY,
        issue_time="latest",
        variables=["q_down"],
        scenarios=["GFS"],
        include_analysis=True,
    )

    assert [segment["scenario_id"] for segment in response["segments"]] == ["analysis_true_field"]
    assert response["segments"][0]["data"] == [{"valid_time": "2026-08-31T23:00:00Z", "value": 2.0}]
    # cycle discovery, analysis rows, forecast rows — all three still executed.
    assert len(_fact_reads(store)) == 3


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
        assert_narrow_fact_read(sql, params)
    assert "SELECT DISTINCT ON (rt.valid_time)" in facts[-1][0]
    assert "ORDER BY rt.valid_time, h.end_time DESC, h.created_at DESC" in facts[-1][0]


def test_row_arrival_order_does_not_change_the_exact_forecast_payload():
    # Two independent result snapshots carrying the SAME values in DIFFERENT
    # order — the shape that used to stand for "all-legacy rows" vs "all-narrow
    # rows" before #1342's contract (task 6.3) left one store. What it still
    # proves is the half that mattered: response sorting is the serializer's
    # job, not the cursor's. This double does not evaluate SQL.
    ascending_rows = [
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
    descending_rows = [
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
    for result_rows in (ascending_rows, descending_rows):
        store = SqlCaptureForecastStore(_target_rows() + [RESOLVED_RUNS[:1], result_rows])
        response = store.forecast_series(
            **IDENTITY,
            issue_time="2026-09-01T06:00:00Z",
            variables=["q_down"],
            scenarios=["GFS"],
        )
        assert response == expected
        facts = [
            (sql, params)
            for sql, params in store.cursor.executions
            if "FROM hydro.river_timeseries rt" in sql
        ]
        assert len(facts) == 1
        outer = assert_narrow_fact_read(*facts[0])
        assert "h.cycle_time = %(issue_time)s" in outer
        assert facts[0][1]["pushdown_run_keys"] == [101]
