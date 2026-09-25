"""#2424 D1: the shape of ``issue_time=latest`` cycle discovery, offline.

A SQL-shape double: the capture cursor records the statement the real
``_per_source_latest_cycles`` executes and answers with rows chosen here. What a
text oracle can own is the SHAPE the design commits to (design.md D1, tasks.md
2.1): ``hydro.hydro_run`` drives the statement and is read once, the fact table
is touched only inside the correlated membership ``EXISTS``, the #2451 key
spelling survives there, the sort fence and the outer ``ORDER BY`` before
``LIMIT 1`` are both present, and nothing filters on ``status``. Whether the
statement SELECTS the same cycles as before is a database question; that is
``tests/test_latest_cycle_discovery_integration.py`` and the node-27 regression.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from packages.common import forecast_store
from packages.common.river_ts_render import render_river_ts_sql
from tests.latest_cycle_discovery_oracle import PRE_2424_PER_SOURCE_LATEST_CYCLES_SQL, pre_2424_statement
from tests.river_ts_template_registry import FORECAST_STORE_EXECUTIONS, entry_by_key
from tests.test_river_ts_text_identity_cleanup import _CaptureCursor

IDENTITY = {"basin_version_id": "basin_v1", "segment_id": "seg_001", "river_network_version_id": "rivnet_v1"}
NO_FILTER = forecast_store._ScenarioFilter("", {})

#: The fact-side conjuncts of the membership probe, in design.md D1's spelling.
#: The basin/network pair is #2451 C1's guarded non-sargable form; segment and
#: variable keep `=`.
FACT_PROBE_CONJUNCTS = (
    "rt.run_key = o.run_key",
    "rt.river_segment_key = seg.river_segment_key",
    "rt.basin_version_key IS NOT NULL",
    "rt.basin_version_key IS NOT DISTINCT FROM seg.basin_version_key",
    "rt.river_network_version_key IS NOT NULL",
    "rt.river_network_version_key IS NOT DISTINCT FROM seg.river_network_version_key",
    "rt.variable_e = 'q_down'::hydro.river_variable",
)

#: sha256 of the statement master ``64f47adee`` executed. Recomputed from master's
#: bytes, not from this tree: load ``git show 64f47adee:packages/common/forecast_store.py``
#: as a module, call its ``PsycopgForecastStore._per_source_latest_cycles`` on a
#: capture cursor with ``_ScenarioFilter("{scenario_filter}", {})`` and
#: ``_ScenarioFilter("{identity_filter}", {})`` as the two filters, and hash the one
#: captured statement (UTF-8). A digest, not a re-derivation from the live
#: ``_segment_rows_source_sql()``: the oracle must stay master's text even when the
#: shared template legitimately moves on.
PRE_2424_MASTER_STATEMENT_SHA256 = "2448bae0243658623d2217e2c8b9f85aaab16e21237aede87bc247e3b002b4c1"


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _balanced(sql: str, open_index: int) -> str:
    """The text inside the parenthesis opening at ``open_index``."""
    assert sql[open_index] == "("
    depth = 0
    for index in range(open_index, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return sql[open_index + 1 : index]
    raise AssertionError("unbalanced parenthesis")


def candidate_cte(sql: str) -> str:
    flat = _flat(sql)
    return _balanced(flat, flat.index("cand AS MATERIALIZED (") + len("cand AS MATERIALIZED "))


def fact_probe(sql: str) -> str:
    flat = _flat(sql)
    return _balanced(flat, flat.index("WHERE EXISTS (") + len("WHERE EXISTS ")).strip()


def assert_latest_cycle_discovery(sql: str, params: Mapping) -> None:
    """Every shape commitment of design.md D1, on one executed statement."""
    flat = _flat(sql)
    assert set(re.findall(r"%\((\w+)\)s", sql)) == set(params)
    # hydro_run drives, and is read exactly once — in the MATERIALIZED candidates.
    assert flat.count("hydro.hydro_run") == 1
    candidates = candidate_cte(sql)
    assert "FROM hydro.hydro_run h" in candidates
    assert "h.run_type = 'forecast'" in candidates
    assert "h.cycle_time IS NOT NULL" in candidates
    assert "h.basin_version_id = %(basin_version_id)s" in candidates
    # User decision (a): no status predicate anywhere, in any spelling.
    assert "status" not in flat.lower()
    # The fact table is touched once, and only inside the correlated EXISTS.
    probe = fact_probe(sql)
    assert flat.count("hydro.river_timeseries") == 1
    assert "FROM hydro.river_timeseries rt" in probe
    for conjunct in FACT_PROBE_CONJUNCTS:
        assert conjunct in probe, conjunct
    assert "rt.basin_version_key =" not in probe
    assert "rt.river_network_version_key =" not in probe
    assert not re.search(
        r"rt\.(run_id|basin_version_id|river_segment_id|river_network_version_id|variable|unit|quality_flag)\b",
        probe,
    )
    # No fact scan feeds an aggregate: the old MAX/GROUP BY shape is gone.
    assert "MAX(" not in flat
    assert "GROUP BY" not in flat
    # Sort first behind the fence, probe lazily, and keep the pick correct under
    # any plan with the outer ORDER BY before LIMIT 1.
    fence = "ORDER BY c.cycle_time DESC OFFSET 0 ) o WHERE EXISTS ("
    assert flat.count(fence) == 1
    assert flat.index("FROM cand c WHERE c.scenario_id = scen.scenario_id") < flat.index(fence)
    tail = flat[flat.index(fence) + len(fence) :]
    assert tail.count("ORDER BY o.cycle_time DESC LIMIT 1 ) pick") == 1
    assert flat.endswith("ORDER BY scen.scenario_id")


def _capture(scenario_filter, identity_filter, rows=None):
    cursor = _CaptureCursor([rows or []])
    result = forecast_store.PsycopgForecastStore("postgresql://unit-test")._per_source_latest_cycles(
        cursor, **IDENTITY, scenario_filter=scenario_filter, identity_filter=identity_filter
    )
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    return sql, params, result


def test_unfiltered_latest_cycle_discovery_is_driven_from_hydro_run():
    sql, params = FORECAST_STORE_EXECUTIONS["per_source_latest_cycles"]()
    assert_latest_cycle_discovery(sql, params)
    assert params == {
        "basin_version_id": "basin_v1",
        "river_segment_id": "seg_001",
        "river_network_version_id": "rivnet_v1",
    }


@pytest.mark.parametrize(
    ("scenarios", "run_id", "model_id"),
    [(["GFS"], None, None), (["GFS", "IFS"], None, "model_a"), (["IFS"], "run_a", "model_a")],
)
def test_scenario_and_identity_filters_are_rendered_once_inside_the_candidates(scenarios, run_id, model_id):
    scenario_filter = forecast_store._scenario_filter(scenarios)
    identity_filter = forecast_store._run_identity_filter(run_id=run_id, model_id=model_id)
    sql, params, _ = _capture(scenario_filter, identity_filter)
    assert_latest_cycle_discovery(sql, params)
    flat = _flat(sql)
    candidates = candidate_cte(sql)
    for fragment in (scenario_filter.sql, identity_filter.sql):
        if not fragment:
            continue
        assert flat.count(fragment) == 1, fragment
        assert fragment in candidates, fragment
    assert params == {
        "basin_version_id": "basin_v1",
        "river_segment_id": "seg_001",
        "river_network_version_id": "rivnet_v1",
        **scenario_filter.params,
        **identity_filter.params,
    }


def test_the_segment_and_network_keys_resolve_through_the_authority_tables():
    sql, _params, _ = _capture(NO_FILTER, NO_FILTER)
    flat = _flat(sql)
    seg = _balanced(flat, flat.index("WITH seg AS (") + len("WITH seg AS "))
    assert (
        "(SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = %(basin_version_id)s)"
        " AS basin_version_key"
    ) in seg
    assert (
        "(SELECT river_segment_key FROM core.river_segment WHERE river_segment_id = %(river_segment_id)s"
        " AND river_network_version_id = %(river_network_version_id)s) AS river_segment_key"
    ) in seg
    assert (
        "(SELECT river_network_version_key FROM core.river_network_version"
        " WHERE river_network_version_id = %(river_network_version_id)s) AS river_network_version_key"
    ) in seg


def test_rows_become_a_per_scenario_cycle_mapping_in_utc():
    rows = [
        {"scenario_id": "forecast_gfs_deterministic", "cycle_time": datetime(2026, 9, 24, 6, tzinfo=UTC)},
        {"scenario_id": "forecast_ifs_deterministic", "cycle_time": datetime(2026, 9, 24, tzinfo=UTC)},
        {"scenario_id": None, "cycle_time": datetime(2026, 9, 25, tzinfo=UTC)},
        {"scenario_id": "forecast_orphan", "cycle_time": None},
    ]
    _sql, _params, result = _capture(NO_FILTER, NO_FILTER, rows)
    assert result == {
        "forecast_gfs_deterministic": datetime(2026, 9, 24, 6, tzinfo=UTC),
        "forecast_ifs_deterministic": datetime(2026, 9, 24, tzinfo=UTC),
    }


def test_no_candidates_mean_no_cycles():
    assert _capture(NO_FILTER, NO_FILTER, [])[2] == {}


def test_the_fact_probe_is_a_registered_narrow_template():
    """The EXISTS body is owned by the renderer and the registry, like every read."""
    entry = entry_by_key("forecast_store:latest_cycle_fact_probe")
    template = entry.source("narrow")
    assert render_river_ts_sql(template, "narrow").sql == template
    sql, _params, _ = _capture(NO_FILTER, NO_FILTER)
    assert _flat(template) == fact_probe(sql)
    with pytest.raises(ValueError, match="Invalid river timeseries store"):
        forecast_store._latest_cycle_fact_probe_template("legacy")


def test_the_frozen_oracle_is_byte_for_byte_the_statement_master_ran():
    """Evidence Floor 1's oracle must stay master's text; an edit to it is red here."""
    digest = hashlib.sha256(PRE_2424_PER_SOURCE_LATEST_CYCLES_SQL.encode()).hexdigest()
    assert digest == PRE_2424_MASTER_STATEMENT_SHA256


def test_the_frozen_oracle_cannot_collapse_into_the_new_statement():
    """The old shape (a fact scan feeding MAX/GROUP BY) is what the oracle keeps."""
    old = pre_2424_statement(scenario_filter=NO_FILTER, identity_filter=NO_FILTER)
    new, _params, _ = _capture(NO_FILTER, NO_FILTER)
    assert old != new
    assert "MAX(h.cycle_time)" in old
    assert "GROUP BY h.scenario_id" in old
    assert "hydro.river_timeseries" in old
    assert "cand AS MATERIALIZED" not in old
