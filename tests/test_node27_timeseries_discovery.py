"""Catalog membership is the lifecycle discovery oracle."""

import pytest

from packages.common.node27_container_contract import SUPERVISED_HYPERTABLES
from packages.common.node27_timeseries_discovery import (
    RUNTIME_HYPERTABLES_SQL,
    discover_hypertables,
    render_hypertable_pairs,
)
from scripts import node27_timeseries_compression_capture as capture
from scripts import node27_timeseries_compression_live_evidence as evidence
from scripts import node27_timeseries_compression_supervisor as supervisor

CANONICAL = (("hydro", "river_timeseries"), ("met", "forcing_station_timeseries"))


@pytest.mark.parametrize(
    ("siblings", "expected"),
    [
        ([], CANONICAL),
        ([("hydro", "river_timeseries_legacy")], CANONICAL + (("hydro", "river_timeseries_legacy"),)),
        (
            [("met", "forcing_station_timeseries_legacy")],
            CANONICAL + (("met", "forcing_station_timeseries_legacy"),),
        ),
        (
            [("hydro", "river_timeseries_legacy"), ("met", "forcing_station_timeseries_legacy")],
            CANONICAL + (("hydro", "river_timeseries_legacy"), ("met", "forcing_station_timeseries_legacy")),
        ),
    ],
)
def test_catalog_membership(siblings, expected):
    assert discover_hypertables(siblings) == expected


def test_dropped_or_ordinary_legacy_is_not_discovered():
    # Ordinary relations are not rows of timescaledb_information.hypertables.
    assert discover_hypertables([]) == CANONICAL
    assert discover_hypertables([("public", "river_timeseries_legacy")]) == CANONICAL


def test_pre_expand_statistics_pairs_are_unchanged():
    assert render_hypertable_pairs(discover_hypertables([])) == (
        "('hydro','river_timeseries'),('met','forcing_station_timeseries')"
    )


def test_capture_runtime_sql_uses_shared_discovery_and_keeps_frozen_allowlists():
    assert RUNTIME_HYPERTABLES_SQL in capture._CATALOG_BODY_SQL
    assert RUNTIME_HYPERTABLES_SQL in capture._selection_sql("selection_pre")
    assert RUNTIME_HYPERTABLES_SQL in capture._sizes_sql("sizes_pre")
    assert RUNTIME_HYPERTABLES_SQL in supervisor._CHECKPOINT_CATALOG_SQL
    assert "UNION ALL" in RUNTIME_HYPERTABLES_SQL
    for sql in (
        capture._CATALOG_BODY_SQL,
        capture._selection_sql("selection_pre"),
        capture._sizes_sql("sizes_pre"),
        supervisor._CHECKPOINT_CATALOG_SQL,
    ):
        assert "UNION ALL" in sql
    assert capture.HYPERTABLE_KEYS == SUPERVISED_HYPERTABLES == evidence.HYPERTABLE_KEYS
    assert evidence.EXPECTED_LAG_SECONDS == 604800
    assert evidence.EXPECTED_BOUND == 1
