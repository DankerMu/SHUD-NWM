from __future__ import annotations

import inspect
import logging
import re
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from apps.api import display_cache, main
from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from packages.common.river_ts_render import render_river_ts_sql
from scripts.node27_raw_retention import DEFAULT_RETENTION_DAYS
from services.tiles import mvt as mvt_module
from services.tiles.mvt import (
    MVT_MAX_COORDINATES,
    NATIONAL_DISCHARGE_QUERY_VERSION,
    TileInput,
    TileResponse,
    cache_key,
    canonical_mvt_time,
    layer_metadata,
    national_discharge_cycles,
    national_discharge_source_version,
    national_discharge_valid_times,
    national_river_network_source_version,
    postgis_tile_sql,
)
from tests.river_ts_template_registry import entry_by_key


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.first_count = 0

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        self.first_count += 1
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows: list[dict[str, Any]], dialect: str = "postgresql") -> None:
        self.rows = rows
        self.sql = ""
        self.executions: list[tuple[str, Any]] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))
        self.results: list[_Rows] = []

    def execute(self, statement: Any, _params: Any = None) -> _Rows:
        self.sql = str(statement)
        self.executions.append((self.sql, _params))
        result = _Rows(self.rows)
        self.results.append(result)
        return result

    def get_bind(self) -> Any:
        return self.bind


def test_national_source_generations_change_with_data_identity() -> None:
    first = _Session(
        [
            {
                "run_id": "run_a",
                "river_network_version_id": "rnv_a",
                "cycle_time": "2026-07-20T00:00:00Z",
                "updated_at": "2026-07-20T01:00:00Z",
            }
        ]
    )
    second = _Session([{**first.rows[0], "run_id": "run_b"}])

    first_version = national_discharge_source_version(first)

    assert first_version.startswith(f"hydro-national:{NATIONAL_DISCHARGE_QUERY_VERSION}:")
    assert first_version != national_discharge_source_version(second)
    assert "ROW_NUMBER() OVER" in first.sql
    assert "ORDER BY h.cycle_time DESC, h.run_id DESC" in first.sql
    assert "AND mi.active_flag" in first.sql
    assert "mi.basin_version_id = h.basin_version_id" in first.sql
    assert "hydro.run_display_coverage" in first.sql
    assert "mi.model_id = h.model_id" not in first.sql


def test_national_valid_times_use_active_basin_identity_not_transient_model_id() -> None:
    # #2009: the coverage query now returns one row per (network, cycle), so the
    # rows carry `cycle_time` and `rn-a` has three of them. The no-argument branch
    # must still answer the pre-#2009 question -- each network's OVERALL latest
    # run -- which it now decides in Python. `rn-a`'s newest cycle is deliberately
    # NEITHER the first nor the last row of its group, and the two stale windows
    # would each move the asserted list, so "take whichever row arrived first" and
    # "take whichever arrived last" are both red here, not just "take them all".
    session = _Session(
        [
            {
                "run_id": "run-a-stale",
                "basin_version_id": "bv-a",
                "river_network_version_id": "rn-a",
                "cycle_time": "2026-07-11T00:00:00Z",
                "segment_count": 2,
                "river_sample_count": 4,
                "river_valid_time_start": "2026-07-11T05:00:00Z",
                "river_valid_time_end": "2026-07-11T06:00:00Z",
                "min_lead_time_hours": 0,
                "max_lead_time_hours": 1,
            },
            {
                "run_id": "run-a",
                "basin_version_id": "bv-a",
                "river_network_version_id": "rn-a",
                "cycle_time": "2026-07-11T06:00:00Z",
                "segment_count": 2,
                "river_sample_count": 8,
                "river_valid_time_start": "2026-07-11T08:00:00Z",
                "river_valid_time_end": "2026-07-11T11:00:00Z",
                "min_lead_time_hours": 0,
                "max_lead_time_hours": 3,
            },
            {
                "run_id": "run-a-middle",
                "basin_version_id": "bv-a",
                "river_network_version_id": "rn-a",
                "cycle_time": "2026-07-11T03:00:00Z",
                "segment_count": 2,
                "river_sample_count": 6,
                "river_valid_time_start": "2026-07-11T05:00:00Z",
                "river_valid_time_end": "2026-07-11T07:00:00Z",
                "min_lead_time_hours": 0,
                "max_lead_time_hours": 2,
            },
            {
                "run_id": "run-b",
                "basin_version_id": "bv-b",
                "river_network_version_id": "rn-b",
                "cycle_time": "2026-07-11T06:00:00Z",
                "segment_count": 3,
                "river_sample_count": 9,
                "river_valid_time_start": "2026-07-11T09:00:00Z",
                "river_valid_time_end": "2026-07-11T11:00:00Z",
                "min_lead_time_hours": 0,
                "max_lead_time_hours": 2,
            },
        ]
    )

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == [
        "2026-07-11T09:00:00Z",
        "2026-07-11T10:00:00Z",
        "2026-07-11T11:00:00Z",
    ]
    assert discovery.observed_count == 3
    assert "mi.basin_version_id = h.basin_version_id" in session.sql
    assert "hydro.run_display_coverage" in session.sql
    assert "mi.model_id = h.model_id" not in session.sql
    assert "hydro.river_timeseries" not in session.sql


def test_national_valid_times_fail_closed_for_non_rectangular_coverage() -> None:
    session = _Session(
        [
            {
                "run_id": "run-a",
                "basin_version_id": "bv-a",
                "river_network_version_id": "rn-a",
                "cycle_time": "2026-07-11T06:00:00Z",
                "segment_count": 2,
                "river_sample_count": 7,
                "river_valid_time_start": "2026-07-11T08:00:00Z",
                "river_valid_time_end": "2026-07-11T11:00:00Z",
                "min_lead_time_hours": 0,
                "max_lead_time_hours": 3,
            }
        ]
    )

    assert national_discharge_valid_times(session).valid_times == []


def test_display_db_pool_bounds_invalid_environment(monkeypatch: Any) -> None:
    monkeypatch.setenv("NHMS_DISPLAY_DB_POOL_SIZE", "1000")
    monkeypatch.setenv("NHMS_DISPLAY_DB_MAX_OVERFLOW", "invalid")

    assert hydro_display._bounded_env_int("NHMS_DISPLAY_DB_POOL_SIZE", default=4, minimum=1, maximum=16) == 4
    assert hydro_display._bounded_env_int("NHMS_DISPLAY_DB_MAX_OVERFLOW", default=2, minimum=0, maximum=16) == 2


def test_systemd_workers_receive_shared_file_cache_default() -> None:
    unit = (Path(__file__).resolve().parents[1] / "infra/systemd/nhms-display-api.service").read_text(
        encoding="utf-8"
    )

    assert 'export NHMS_MVT_FILE_CACHE_DIR="${NHMS_MVT_FILE_CACHE_DIR:-/home/nwm/.cache/nhms/mvt}"' in unit
    assert '--workers "${NHMS_DISPLAY_WORKERS:-2}"' in unit


def test_national_river_generation_uses_only_active_network_inventory() -> None:
    session = _Session(
        [
            {
                "river_network_version_id": "rnv_a",
                "basin_version_id": "bv_a",
                "segment_count": 10,
                "checksum": "abc",
                "created_at": "2026-07-20T00:00:00Z",
            }
        ]
    )

    version = national_river_network_source_version(session)

    assert version.startswith("river-network-national:stream-type-aggregate-v3:")
    assert "mi.active_flag = true" in session.sql
    assert "ORDER BY rnv.river_network_version_id" in session.sql


def test_national_river_metadata_is_versioned_pbf() -> None:
    first = layer_metadata("river-network", source_version="generation-a", national=True)
    second = layer_metadata("river-network", source_version="generation-b", national=True)

    assert first["tile_url_template"] == "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf"
    assert first["maplibre_source_layer"] == "river_network"
    assert first["source_generation"] == "generation-a"
    assert first["cache_version"] != second["cache_version"]


def test_national_queries_filter_stream_type_before_geometry_materialization() -> None:
    river_sql = postgis_tile_sql("river-network-national")
    hydro_sql = postgis_tile_sql("hydro-national")

    assert "mi.active_flag = true" in river_sql
    assert 'rs.stream_type AS "Type"' in river_sql
    assert "OR rs.stream_type >= CASE" in river_sql
    assert "ST_LineMerge(ST_Collect(geom))" in river_sql
    assert "WHERE :z <= 8" in river_sql
    assert "WHERE :z >= 9" in river_sql
    assert "tile_segments AS MATERIALIZED" in hydro_sql
    assert hydro_sql.count("AND mi.active_flag") >= 2
    assert hydro_sql.count("mi.basin_version_id = h.basin_version_id") >= 2
    assert hydro_sql.count("hydro.run_display_coverage") >= 2
    assert "mi.model_id = h.model_id" not in hydro_sql
    assert "rdc.river_valid_time_start <= :valid_time" in hydro_sql
    assert "rdc.river_valid_time_end >= :valid_time" in hydro_sql
    assert hydro_sql.index("selected_values AS") < hydro_sql.rindex("JOIN core.river_segment rs")
    assert "network_stream_max AS MATERIALIZED" in hydro_sql
    assert "MAX(rs0.stream_type) AS max_stream_type" in hydro_sql
    assert "LEAST(" in hydro_sql
    assert "nsm.max_stream_type" in hydro_sql
    assert "seg.stream_type IS NULL" in hydro_sql


def test_national_hydro_tile_fairly_caps_rows_before_the_shared_mvt_budget() -> None:
    hydro_sql = postgis_tile_sql("hydro-national")
    single_run_sql = postgis_tile_sql("hydro")

    assert "national_ranked AS" in hydro_sql
    assert "PARTITION BY river_network_version_id" in hydro_sql
    assert "ORDER BY network_rank, value DESC NULLS LAST," in hydro_sql
    assert "tile_feature_rank <= :feature_limit" in hydro_sql
    assert "tile_coordinate_rank <= :collection_coordinate_limit" in hydro_sql
    assert hydro_sql.index("national_budget_window AS") < hydro_sql.index("budget_stats AS")
    assert "national_budget_window AS" not in single_run_sql


def test_national_river_sql_uses_the_denser_stream_type_table_only_for_the_national_layer() -> None:
    national_sql = postgis_tile_sql("river-network-national")
    per_basin_sql = postgis_tile_sql("river-network")
    hydro_sql = postgis_tile_sql("hydro-national")

    for literal in (
        "WHEN :z <= 4 THEN 4.0",
        "WHEN :z = 5 THEN 3.0",
        "WHEN :z = 6 THEN 2.0",
        "WHEN :z = 7 THEN 1.0",
    ):
        assert literal in national_sql
    assert "WHEN :z <= 4 THEN 5.0" not in national_sql
    assert "WHEN :z = 7 THEN 2.0" not in national_sql

    for literal in (
        "WHEN :z <= 4 THEN 5.0",
        "WHEN :z = 5 THEN 4.0",
        "WHEN :z = 6 THEN 3.0",
        "WHEN :z = 7 THEN 2.0",
    ):
        assert literal in per_basin_sql
    assert "WHEN :z <= 4 THEN 4.0" not in per_basin_sql
    assert "WHEN :z = 7 THEN 1.0" not in per_basin_sql

    assert "WHEN :z <= 4 THEN 5.0" in hydro_sql


def test_national_river_tile_fairly_caps_rows_before_the_shared_mvt_budget() -> None:
    national_sql = postgis_tile_sql("river-network-national")
    per_basin_sql = postgis_tile_sql("river-network")

    assert "preeligible AS" in national_sql
    assert "FROM bounded_rows" in national_sql
    assert "national_ranked AS" in national_sql
    assert "national_budget_window AS" in national_sql
    assert "PARTITION BY river_network_version_id" in national_sql
    assert "tile_feature_rank <= :feature_limit" in national_sql
    assert "tile_coordinate_rank <= :collection_coordinate_limit" in national_sql
    assert national_sql.index("preeligible AS") < national_sql.index("national_budget_window AS")
    assert national_sql.index("national_budget_window AS") < national_sql.index("budget_stats AS")
    # The window ranks the layer's existing eligibility filter, not bounded_rows,
    # so the per-feature and dimension guards keep applying before ranking.
    preeligible_body = national_sql[
        national_sql.index("preeligible AS") : national_sql.index("national_ranked AS")
    ]
    assert "source_coordinate_count <= :feature_coordinate_limit" in preeligible_body
    assert "source_coordinate_dimensions <= :max_coordinate_dimensions" in preeligible_body
    assert "FROM preeligible" in national_sql

    # All three window ORDER BY clauses the budget window introduces -- the
    # per-network `network_rank`, the global `tile_feature_rank` and the running
    # `tile_coordinate_rank` -- must END with the unique river_segment_id
    # tiebreak: at z >= 9 the per-segment rows tie massively on "Type" and the
    # truncation point would otherwise follow the execution plan while the bytes
    # are cached under one generation. `network_rank` matters most: a non-unique
    # rank there changes which rows the outer global order even considers.
    window_start = national_sql.index("national_ranked AS")
    window_block = national_sql[window_start : national_sql.index("eligible AS", window_start)]
    order_by_clauses = re.findall(r"ORDER BY(.*?)(?:\)|ROWS BETWEEN)", window_block, re.S)
    # Exactly three, so a future fourth ordering cannot slip past unasserted.
    assert len(order_by_clauses) == 3
    for clause in order_by_clauses:
        assert clause.strip().endswith("river_segment_id")
        # stream_type is nullable and the z >= 9 arm admits rows regardless of
        # stream class, so a bare DESC (NULLS FIRST in Postgres) would let an
        # unclassified segment outrank every trunk and eat the budget first.
        assert '"Type" DESC NULLS LAST' in clause
    assert re.search(r'"Type"\s+DESC(?!\s+NULLS\s+LAST)', national_sql) is None

    # The leading sort key is the whole point of the window and the checks above
    # are position-insensitive, so pin it. `network_rank` orders each network's
    # own segments by stream class; both GLOBAL orders must then lead with
    # `network_rank` so admission goes round-robin across networks. Leading the
    # global orders with "Type" instead would spend the budget network by
    # network, admitting a dense network's minor tributaries ahead of a sparse
    # network's trunk -- the first-come order the spec forbids.
    normalized = [" ".join(clause.split()) for clause in order_by_clauses]
    assert normalized[0].startswith('"Type" DESC NULLS LAST,')
    for clause in normalized[1:]:
        assert clause.startswith('network_rank, "Type" DESC NULLS LAST,')

    # The running total must sum the per-feature COORDINATE cost over a
    # PRECEDING..CURRENT frame. Summing anything else (a row count, say) makes
    # `tile_coordinate_rank` compare apples to a coordinate limit and restores
    # the 413 the window exists to remove; widening the frame to UNBOUNDED
    # FOLLOWING makes every row carry the tile's grand total, so one over-budget
    # tile empties `eligible` entirely and the route caches a blank 200.
    assert "SUM(source_coordinate_count) OVER (" in window_block
    assert window_block.count("ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW") == 1

    assert "national_budget_window" not in per_basin_sql
    assert "network_rank" not in per_basin_sql
    assert "preeligible" not in per_basin_sql


def test_collection_coordinate_limit_is_raised_only_for_the_national_river_layer() -> None:
    def limits(**kwargs: Any) -> tuple[int, int]:
        params = hydro_display._postgis_tile_params({}, z=6, x=48, y=25, **kwargs)
        return params["collection_coordinate_limit"], params["feature_coordinate_limit"]

    assert limits(layer="river-network-national") == (120000, 50000)
    for layer in ("river-network", "hydro", "hydro-national", "met-stations"):
        assert limits(layer=layer) == (50000, 50000)
    # Existing script and test callers compare the exact binding dictionary and
    # pass no layer; the default must stay on the shared limit.
    assert limits() == (50000, 50000)
    assert limits(layer=None) == (50000, 50000)


def test_production_tile_bind_site_forwards_the_layer_to_the_collection_limit(monkeypatch: Any) -> None:
    # `_fetch_postgis_tile_bytes` is the single production bind site for tile
    # SQL. If it stopped forwarding `layer=layer`, `:collection_coordinate_limit`
    # would bind to the shared limit while the 413 comparison used 120000: the
    # window would truncate every hot national tile at the wrong point, nothing
    # would raise, and no other test would notice. Assert the bound values.
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    layer_params: dict[str, dict[str, Any]] = {
        "river-network-national": {},
        "river-network": {"basin_version_id": "bv_a"},
        "hydro": {"variable": "q_down"},
        "hydro-national": {"variable": "q_down"},
        "met-stations": {"basin_version_id": "bv_a"},
    }
    expected_collection_limits = {
        "river-network-national": 120000,
        "river-network": MVT_MAX_COORDINATES,
        "hydro": MVT_MAX_COORDINATES,
        "hydro-national": MVT_MAX_COORDINATES,
        "met-stations": MVT_MAX_COORDINATES,
    }

    for layer, params in layer_params.items():
        session = _Session([_budget_row(10)])

        assert hydro_display._fetch_postgis_tile_bytes(session, layer, params, z=6, x=48, y=25) == b"pbf-bytes"

        assert len(session.executions) == 1
        bound = session.executions[0][1]
        assert bound is not None, layer
        assert bound["collection_coordinate_limit"] == expected_collection_limits[layer], layer
        assert bound["feature_coordinate_limit"] == MVT_MAX_COORDINATES, layer


# #2030: the tile route's logger tree; `apps.api.routes.hydro_display` propagates
# to the root, which is where `caplog` attaches.
_TILE_ROUTE_LOGGER = "apps.api.routes.hydro_display"
_TILE_LAYERS = ("river-network", "river-network-national", "hydro", "hydro-national", "met-stations")


def _truncation_records(caplog: Any) -> list[Any]:
    return [record for record in caplog.records if "MVT_TILE_BUDGET_TRUNCATED" in record.getMessage()]


def _final_select_text(layer: str) -> str:
    """Text of `postgis_tile_sql(layer)` after the one final outer `SELECT ST_AsMVT(`.

    `ST_AsMVTGeom` inside the `clipped` CTE is not preceded by `SELECT `, so the
    literal is unique and the slice is exactly the outer projection list.
    """
    sql = postgis_tile_sql(layer)
    assert sql.count("SELECT ST_AsMVT(") == 1, layer
    return sql.split("SELECT ST_AsMVT(", 1)[1]


def _budget_row(coordinate_count: int) -> dict[str, Any]:
    # #2030: the four truncation-signal columns default to the untruncated state
    # (intersecting == selected, no overflow), so every pre-existing caller of
    # this helper keeps its 413/200 verdict *and* stays silent.
    return {
        "tile": b"pbf-bytes",
        "feature_count": 12,
        "coordinate_count": coordinate_count,
        "source_identity_count": 1,
        "invalid_property_count": 0,
        "invalid_properties": "",
        "intersecting_feature_count": 12,
        "intersecting_coordinate_count": coordinate_count,
        "feature_coordinate_overflow_count": 0,
        "coordinate_dimension_overflow_count": 0,
    }


def test_national_river_tile_over_the_shared_limit_but_within_its_own_is_rendered(
    monkeypatch: Any, caplog: Any
) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_budget_row(119_999)])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        tile = hydro_display._fetch_postgis_tile_bytes(session, "river-network-national", {}, z=3, x=6, y=3)

    assert tile == b"pbf-bytes"
    # #2030: a big-but-untruncated tile must not cry wolf.
    assert _truncation_records(caplog) == []


def test_national_river_tile_above_its_own_limit_still_raises_413_against_that_limit(monkeypatch: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_budget_row(120_001)])

    with pytest.raises(ApiError) as excinfo:
        hydro_display._fetch_postgis_tile_bytes(session, "river-network-national", {}, z=3, x=6, y=3)

    assert excinfo.value.status_code == 413
    assert excinfo.value.code == "MVT_TILE_BUDGET_EXCEEDED"
    assert excinfo.value.details["max_coordinates"] == 120000
    assert excinfo.value.details["coordinate_count"] == 120_001


def test_national_hydro_tile_keeps_the_shared_413_limit(monkeypatch: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_budget_row(60_000)])

    with pytest.raises(ApiError) as excinfo:
        hydro_display._fetch_postgis_tile_bytes(
            session, "hydro-national", {"variable": "q_down"}, z=3, x=6, y=3
        )

    assert excinfo.value.status_code == 413
    assert excinfo.value.details["max_coordinates"] == 50000


def test_per_basin_river_tile_keeps_the_shared_413_limit(monkeypatch: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_budget_row(60_000)])

    with pytest.raises(ApiError) as excinfo:
        hydro_display._fetch_postgis_tile_bytes(
            session, "river-network", {"basin_version_id": "bv_a"}, z=7, x=101, y=52
        )

    assert excinfo.value.status_code == 413
    assert excinfo.value.details["max_coordinates"] == 50000


# #2030: budget-window layers compute `budget_stats` FROM the already truncated
# `eligible`, so the 413 predicate is unreachable there and over-budget tiles are
# a silent 200 with fewer rows. These stub rows drive the four columns the route
# now reads and pin both the firing and the silent boundaries.
_TRUNCATION_CASES = (
    pytest.param(
        "river-network-national",
        {},
        {
            "feature_count": 23,
            "intersecting_feature_count": 23,
            "coordinate_count": 38531,
            "intersecting_coordinate_count": 38531,
        },
        (),
        {},
        id="a-equal-counts-are-silent",
    ),
    pytest.param(
        "river-network-national",
        {},
        {
            "feature_count": 23,
            "intersecting_feature_count": 56,
            "coordinate_count": 38531,
            "intersecting_coordinate_count": 86160,
        },
        (
            "MVT_TILE_BUDGET_TRUNCATED",
            "layer_id=river-network-national",
            "z=3 x=6 y=3",
            "feature_count=23/56",
            "max_features=10000",
            "coordinate_count=38531/86160",
            "max_coordinates=120000",
        ),
        {"layer_id": "river-network-national", "intersecting_coordinate_count": 86160},
        id="b-both-arms-fire",
    ),
    pytest.param(
        "river-network-national",
        {},
        {
            "feature_count": 23,
            "intersecting_feature_count": 24,
            "coordinate_count": 38531,
            "intersecting_coordinate_count": 38531,
        },
        ("MVT_TILE_BUDGET_TRUNCATED", "feature_count=23/24", "coordinate_count=38531/38531"),
        {},
        id="c-feature-arm-alone-fires",
    ),
    pytest.param(
        "river-network-national",
        {},
        {
            "feature_count": 23,
            "intersecting_feature_count": 23,
            "coordinate_count": 38531,
            "intersecting_coordinate_count": 86160,
            "feature_coordinate_overflow_count": 1,
        },
        (),
        {},
        id="d-per-feature-coordinate-overflow-is-silent",
    ),
    pytest.param(
        "river-network-national",
        {},
        {
            "feature_count": 23,
            "intersecting_feature_count": 23,
            "coordinate_count": 38531,
            "intersecting_coordinate_count": 86160,
            "coordinate_dimension_overflow_count": 1,
        },
        (),
        {},
        id="e-coordinate-dimension-overflow-is-silent",
    ),
    pytest.param(
        "hydro-national",
        {"variable": "q_down"},
        {
            "feature_count": 23,
            "intersecting_feature_count": 23,
            "coordinate_count": 40000,
            "intersecting_coordinate_count": 60000,
        },
        (
            "MVT_TILE_BUDGET_TRUNCATED",
            "layer_id=discharge",
            "z=3 x=6 y=3",
            "coordinate_count=40000/60000",
            "max_coordinates=50000",
        ),
        {"layer_id": "discharge", "max_coordinates": 50000},
        id="f-hydro-national-uses-the-public-layer-id",
    ),
)


@pytest.mark.parametrize(("layer", "params", "overrides", "fragments", "attrs"), _TRUNCATION_CASES)
def test_budget_truncation_signal_matrix(
    monkeypatch: Any,
    caplog: Any,
    layer: str,
    params: dict[str, Any],
    overrides: dict[str, Any],
    fragments: tuple[str, ...],
    attrs: dict[str, Any],
) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([{**_budget_row(0), **overrides}])

    with caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER):
        tile = hydro_display._fetch_postgis_tile_bytes(session, layer, params, z=3, x=6, y=3)

    assert tile == b"pbf-bytes"
    records = _truncation_records(caplog)
    if not fragments:
        assert records == []
        return
    assert len(records) == 1
    # The spec pins the severity, not just the token: an ERROR would page an
    # operator for a tile that still rendered.
    assert records[0].levelno == logging.WARNING
    # The name is load-bearing: only the `apps.api` tree reaches the stderr handler
    # in apps/api/main.py that feeds /tmp/display-api.log, and caplog sits on the root.
    assert records[0].name == _TILE_ROUTE_LOGGER
    message = records[0].getMessage()
    for fragment in fragments:
        assert fragment in message, (fragment, message)
    for name, value in attrs.items():
        assert getattr(records[0], name) == value, name


def test_every_tile_layer_projects_the_prefilter_intersecting_counts() -> None:
    # The bare `AS intersecting_*` aliases already exist once inside the
    # `prefilter_stats` CTE, so this counts the full projection expression in the
    # final SELECT slice only.
    for layer in _TILE_LAYERS:
        final_select = _final_select_text(layer)
        for column in ("intersecting_feature_count", "intersecting_coordinate_count"):
            projection = f"(SELECT {column} FROM prefilter_stats) AS {column}"
            assert final_select.count(projection) == 1, (layer, column)


def test_every_column_the_tile_route_reads_is_projected_by_every_layer() -> None:
    # Route -> SQL coverage lock: read the keys out of the live route source
    # rather than restating them, so a column the route starts reading (or a
    # layer that stops projecting one) fails here instead of at runtime.
    source = inspect.getsource(hydro_display._fetch_postgis_tile_bytes)
    keys = set(re.findall(r'row\.get\("([a-z_]+)"\)', source)) | set(re.findall(r'row\["([a-z_]+)"\]', source))

    assert keys, "no row column reads found in _fetch_postgis_tile_bytes"
    assert {
        "tile",
        "feature_count",
        "coordinate_count",
        "source_identity_count",
        "invalid_property_count",
        "invalid_properties",
        "intersecting_feature_count",
        "intersecting_coordinate_count",
        "feature_coordinate_overflow_count",
        "coordinate_dimension_overflow_count",
    } <= keys

    for layer in _TILE_LAYERS:
        final_select = _final_select_text(layer)
        for key in sorted(keys):
            # `\b` so `AS tile` does not match `AS tile_rows` and `AS feature_count`
            # does not match `AS feature_coordinate_count`.
            assert re.search(rf"\bAS {re.escape(key)}\b", final_select), (layer, key)


def test_concurrent_cold_requests_generate_one_tile(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    tile = TileInput(
        layer_id="discharge",
        source_id="hydro-national",
        source_version="generation-a",
        valid_time="2026-07-20T00:00:00Z",
        z=3,
        x=6,
        y=3,
    )
    calls = 0
    reads = 0
    stored: TileResponse | None = None
    state_lock = threading.Lock()
    first_reads = threading.Barrier(2)

    def fake_read(_session: object, _tile: TileInput) -> TileResponse | None:
        nonlocal reads
        with state_lock:
            reads += 1
            current_read = reads
            current = stored
        if current_read <= 2:
            first_reads.wait(timeout=2)
            return None
        return current

    def fake_build(_session: object, _tile: TileInput, data: bytes) -> TileResponse:
        nonlocal stored
        response = TileResponse(
            data=data,
            checksum="checksum",
            etag='W/"etag"',
            cache_key="key",
            cache_status="miss",
            layer_id="discharge",
        )
        with state_lock:
            stored = response
        return response

    def produce() -> bytes:
        nonlocal calls
        with state_lock:
            calls += 1
        time.sleep(0.05)
        return b"pbf"

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)

    responses: list[Any] = []

    def request() -> None:
        responses.append(hydro_display._cached_or_generated_mvt_response(object(), tile, produce))

    threads = [threading.Thread(target=request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert len(responses) == 2
    assert {response.headers["x-tile-checksum"] for response in responses} == {"checksum"}
    assert stored is not None and stored.data == b"pbf"


# --- #2007: the hydro-national {source}/{cycle} identity -------------------
#
# `postgis_tile_sql` keeps its single-argument signature, so the identity
# travels as the named binds `:source` / `:cycle`. Two independent run
# selections consume them and they must agree, because one produces the tile's
# rows and the other produces the 0/1 the route turns into 200 vs 424.

_NATIONAL_CYCLE = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
_NATIONAL_VALID_TIME = datetime(2026, 9, 3, 0, 0, tzinfo=UTC)
_NATIONAL_TILE_Z = 4
_NATIONAL_TILE_X = 13
_NATIONAL_TILE_Y = 6
_NATIONAL_ROUTE_PREFIX = "/api/v1/tiles/hydro-national"

# The identity pair verbatim, INCLUDING the leading `AND (`. Asserting only the
# inner `CAST(...) IS NULL OR ...` half leaves the conjunct/disjunct distinction
# unpinned, and that distinction is the whole predicate: SQL's AND binds tighter
# than OR, so `... AND mi.active_flag OR (guard OR match) AND (guard OR match)`
# parses as `(everything unbound) OR (both matches)` and admits EVERY candidate
# run again. Measured: with the digest's `AND (` flipped to `OR  (`, all three
# of `national_discharge_source_version(session)`,
# `...(source="gfs", cycle=<early>)` and `...(source="gfs", cycle=<late>)`
# collapse onto one value against a real database, and the whole unit suite
# stayed green before these constants existed.
_SOURCE_CONJUNCT = "AND (CAST(:source AS text) IS NULL OR lower(h.source_id) = :source)"
_CYCLE_CONJUNCT = "AND (CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle)"


def _national_sql_sites() -> tuple[str, str]:
    """The two run-selection slices of the national tile SQL, located not counted.

    A `count(...) >= 2` would pass with both occurrences inside the data CTE,
    which is exactly the bug design D1 warns about: the identity probe would
    then still answer "present" from another source's run and the route would
    serve an empty 200 where the contract requires 424. So each site is sliced
    out by its own landmark and asserted separately.
    """
    sql = postgis_tile_sql("hydro-national")
    cte_start = sql.index("WITH latest_runs AS MATERIALIZED (")
    cte_end = sql.index("network_stream_max AS MATERIALIZED", cte_start)
    probe_start = sql.index("source_identity_stats AS (")
    probe_end = sql.index("AS source_identity_count", probe_start)
    # Non-vacuity: the slices are disjoint and in this order, so neither
    # assertion below can be satisfied by the other site's text.
    assert cte_start < cte_end < probe_start < probe_end
    return sql[cte_start:cte_end], sql[probe_start:probe_end]


def test_national_tile_sql_binds_source_and_cycle_at_both_run_selection_sites() -> None:
    cte_slice, probe_slice = _national_sql_sites()

    for site_name, site in (("latest_runs CTE", cte_slice), ("identity probe", probe_slice)):
        # The NULL guard is what keeps the legacy 5-segment route's run
        # selection unchanged, so it is part of the locked shape. It also splits
        # the two sub-predicates apart, which is why they are located
        # individually and never as one contiguous string. The leading `AND (`
        # is inside the pinned literal: without it a conjunct -> disjunct flip
        # keeps every substring satisfied while the predicate narrows nothing.
        assert _SOURCE_CONJUNCT in site, site_name
        assert _CYCLE_CONJUNCT in site, site_name
        # `:source::text` would make SQLAlchemy's bind regex backtrack and emit
        # a bogus `sourc` bind that no fake-session test can see.
        assert ":source::" not in site, site_name
        assert ":cycle::" not in site, site_name

    binds = set(text(postgis_tile_sql("hydro-national"))._bindparams)
    assert {"source", "cycle"} <= binds
    assert not binds & {"sourc", "cycl"}


# The coverage-window clamp verbatim, `AND (` included, for the same reason the
# two conjuncts above carry theirs: SQL's AND binds tighter than OR, so an
# `AND (` -> `OR  (` flip turns the whole WHERE into "every unbound row, OR the
# matching ones" and re-admits every candidate run while every inner substring
# stays satisfied.
#
# Deliberately NOT byte-identical to the tile CTE's window predicate: that one
# is an unguarded `JOIN ... ON` (the tile always has an instant), this one has
# to stay inert for the instant-less catalog call. The oracle for the pair is
# behavioural -- same run selected when both are bound --
# `tests/test_mvt_national_identity_probe_integration.py`, not string equality.
_VALID_TIME_CONJUNCT_HEAD = "AND ("
_VALID_TIME_GUARD = "CAST(:valid_time AS timestamptz) IS NULL"
_VALID_TIME_WINDOW_START = "rdc.river_valid_time_start <= :valid_time"
_VALID_TIME_WINDOW_END = "rdc.river_valid_time_end >= :valid_time"


def _national_digest_ranked_subquery(sql: str) -> str:
    """The ranked sub-query alone, sliced out by its own landmarks.

    A whole-statement `in` check would be satisfied by the predicate sitting in
    the OUTER `WHERE rn = 1`, and that placement is a real, silent bug rather
    than a style difference: filtering after `ROW_NUMBER()` drops the rank-1 row
    of a network whose latest run misses the instant and leaves that network
    absent from the digest entirely, instead of letting the next candidate
    become rank 1 -- which is exactly the run the tile paints.
    """
    start = sql.index("FROM (")
    end = sql.index(") ranked", start)
    assert start < end
    # Non-vacuity: the slice really is the sub-query, not the whole statement.
    assert "WHERE rn = 1" not in sql[start:end]
    return sql[start:end]


def test_national_digest_clamps_its_ranking_to_the_requested_instants_coverage_window() -> None:
    """#2031: the digest must rank the run `latest_runs` would paint, not the newest one.

    Measured on node-27 (receipt `2026-09-08-issue-2031-digest-precondition.md`):
    the legacy route's digest and its tile disagreed on the selected run for
    every one of the 38 active networks, and the new route has 20 same-cycle
    double-run groups one differing window away from the same split. When they
    disagree, two instants painted from two different runs share one cache key
    and the file tile cache has no TTL.

    The NULL guard is half the contract: `/api/v1/layers` has no instant, and a
    clamp that is not inert when unbound would empty the catalog's digest.
    """
    session = _CapturingSession(list(_NationalRouteSession._DIGEST_ROWS))

    national_discharge_source_version(session, valid_time=_NATIONAL_VALID_TIME)

    ranked = _national_digest_ranked_subquery(session.sql)
    # Inside the ranked sub-query, before ROW_NUMBER() decides rank 1.
    assert _VALID_TIME_GUARD in ranked
    assert _VALID_TIME_WINDOW_START in ranked
    assert _VALID_TIME_WINDOW_END in ranked
    # Conjunct, not disjunct: the guard opens an `AND (`-introduced group.
    guard_at = ranked.index(_VALID_TIME_GUARD)
    assert _VALID_TIME_CONJUNCT_HEAD in ranked[:guard_at][-40:], (
        f"the coverage clamp must be a conjunct; preceding text: {ranked[:guard_at][-40:]!r}"
    )
    # `:valid_time::timestamptz` would make SQLAlchemy's bind regex backtrack
    # and emit a bogus `valid_tim` bind that no fake-session test can see.
    assert ":valid_time::" not in session.sql
    binds = set(text(session.sql)._bindparams)
    assert binds == {"source", "cycle", "valid_time"}, binds
    assert not binds & {"sourc", "cycl", "valid_tim"}


def test_national_digest_always_binds_valid_time_even_when_no_instant_is_given() -> None:
    """`text()` raises on a missing named bind, and no fake session can see that.

    The catalog and every other instant-less caller reach the same statement, so
    omitting the parameter instead of binding NULL is a runtime failure on the
    real driver only -- the exact class of break #2007 left in four integration
    cases.
    """
    session = _CapturingSession(list(_NationalRouteSession._DIGEST_ROWS))

    national_discharge_source_version(session)

    declared = set(text(session.sql)._bindparams)
    supplied = set(session.params[0])
    assert declared - supplied == set(), "the digest omits a bind its own SQL declares"
    assert session.params[0]["valid_time"] is None


def test_national_digests_join_the_network_version_and_project_its_geometry_generation() -> None:
    """#2031's other half: an in-place geometry rewrite must move both keys.

    `_backfill_output_segment_geometry` rewrites `core.river_segment.geom` and
    the STORED `stream_type` UNDER an unchanged network version. No run row
    moves, and `rnv.segment_count` / `rnv.checksum` describe the imported
    package rather than the stored geometry, so before this column both national
    digests were blind to it: the tile repainted and the cache key did not.

    The INNER JOIN drops no candidate -- `core.model_instance.river_network_version_id`
    is `NOT NULL REFERENCES core.river_network_version` (000004_core.sql) -- and it
    aligns the digest's join shape with `latest_runs`.
    """
    discharge = _CapturingSession(list(_NationalRouteSession._DIGEST_ROWS))
    national_discharge_source_version(discharge, source="gfs", cycle=_NATIONAL_CYCLE)

    ranked = _national_digest_ranked_subquery(discharge.sql)
    assert "JOIN core.river_network_version rnv" in ranked
    assert "ON rnv.river_network_version_id = mi.river_network_version_id" in ranked
    # Both projections: the inner one selects it, the OUTER list is explicit and
    # would silently drop the column from the digest basis if it were forgotten.
    assert "rnv.geometry_generation" in ranked
    outer = discharge.sql[: discharge.sql.index("FROM (")]
    assert "geometry_generation" in outer, f"outer projection drops the column: {outer!r}"

    river = _Session(
        [
            {
                "river_network_version_id": "rnv_a",
                "basin_version_id": "bv_a",
                "segment_count": 10,
                "checksum": "abc",
                "geometry_generation": 0,
                "created_at": "2026-07-20T00:00:00Z",
            }
        ]
    )
    national_river_network_source_version(river)
    assert "rnv.geometry_generation" in river.sql
    # The sibling layer's version literal is NOT bumped by this projection
    # change: the basis moves on its own, which is the whole point of D7.
    assert "stream-type-aggregate-v3" in national_river_network_source_version(river)


def test_a_geometry_generation_bump_moves_both_national_digests() -> None:
    """A returned `geometry_generation` reaches the digest basis.

    Fake rows, so this proves the row-consuming half only: `_national_source_digest`
    hashes whatever the query returns, with no column whitelist and no row
    mapping that drops unknown keys -- either of which would keep every SQL-text
    assertion green while the key stood still through a geometry rewrite. Same
    fake rows, one incremented counter, two different digests -- on BOTH layers,
    because the backfill repaints both.

    That the real SQL actually PROJECTS the column is a different claim, pinned
    elsewhere: the SQL-text assertions in this file, and 5.2 on node-27 against
    a live database.
    """
    discharge_rows = [{**_NationalRouteSession._DIGEST_ROWS[0], "geometry_generation": 0}]
    bumped_discharge_rows = [{**discharge_rows[0], "geometry_generation": 1}]
    assert national_discharge_source_version(_Session(discharge_rows)) != (
        national_discharge_source_version(_Session(bumped_discharge_rows))
    )

    river_rows = [
        {
            "river_network_version_id": "rnv_a",
            "basin_version_id": "bv_a",
            "segment_count": 10,
            "checksum": "abc",
            "geometry_generation": 0,
            "created_at": "2026-07-20T00:00:00Z",
        }
    ]
    bumped_river_rows = [{**river_rows[0], "geometry_generation": 1}]
    assert national_river_network_source_version(_Session(river_rows)) != (
        national_river_network_source_version(_Session(bumped_river_rows))
    )
    # Non-vacuity: the same rows twice really do digest identically, so the
    # inequalities above are attributable to the counter and not to nondeterminism.
    assert national_discharge_source_version(_Session(discharge_rows)) == (
        national_discharge_source_version(_Session(list(discharge_rows)))
    )
    assert national_river_network_source_version(_Session(river_rows)) == (
        national_river_network_source_version(_Session(list(river_rows)))
    )


def test_national_discharge_query_version_is_pinned_to_the_literal_the_spec_names() -> None:
    """A literal, not the imported constant.

    Every other assertion on this constant imports it from the module under
    test and interpolates it, so reverting the value would keep them all green
    while silently un-rotating the cache key that #2007's new run selection
    requires. The spec names `fair-network-budget-v5`; this is the only place
    the repo says so.
    """
    assert NATIONAL_DISCHARGE_QUERY_VERSION == "fair-network-budget-v5"


def test_sibling_tile_layers_carry_no_source_or_cycle_bind() -> None:
    for layer in ("hydro", "river-network-national", "river-network", "met-stations"):
        layer_sql = postgis_tile_sql(layer)
        assert ":source" not in layer_sql, layer
        assert ":cycle" not in layer_sql, layer


def test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one() -> None:
    """The digest feeds `source_version`, so it must answer the identity's question.

    Ranking each network's OVERALL latest run would leave the digest — and the
    cache key — unchanged when a non-latest `(source, cycle)` is re-run, and
    this issue is what makes such an identity addressable.
    """
    rows = [
        {
            "run_id": "run_a",
            "river_network_version_id": "rnv_a",
            "cycle_time": "2026-09-02T12:00:00Z",
            "updated_at": "2026-09-02T13:00:00Z",
        }
    ]
    unbound = _CapturingSession(rows)
    bound = _CapturingSession(rows)

    national_discharge_source_version(unbound)
    national_discharge_source_version(bound, source="gfs", cycle=_NATIONAL_CYCLE)

    assert unbound.params == [{"source": None, "cycle": None, "valid_time": None}]
    assert bound.params == [{"source": "gfs", "cycle": _NATIONAL_CYCLE, "valid_time": None}]
    # Same locked literal as the two `postgis_tile_sql` sites, `AND (` included:
    # this helper's narrowing has no fake-session oracle at all (`_Session` never
    # executes SQL), so the shape assertion is the only local guard and a
    # conjunct -> disjunct flip must not pass it. The BEHAVIORAL oracle is
    # `tests/test_mvt_national_identity_probe_integration.py`
    # ::test_national_digest_narrows_the_ranked_runs_to_the_bound_identity.
    assert _SOURCE_CONJUNCT in bound.sql
    assert _CYCLE_CONJUNCT in bound.sql
    # One shared status set, one occurrence: `test_display_publish_status_only`
    # pins the counts this helper contributes.
    assert bound.sql.count("h.status IN ('succeeded', 'parsed', 'published')") == 1


class _CapturingSession(_Session):
    def __init__(self, rows: list[dict[str, Any]], dialect: str = "postgresql") -> None:
        super().__init__(rows, dialect=dialect)
        self.params: list[dict[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        self.params.append(dict(params or {}))
        return super().execute(statement, params)


class _TileResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _TileResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _NationalRouteSession:
    """Answers both statements the national route issues, and records their binds."""

    _DIGEST_ROWS = [
        {
            "run_id": "run_a",
            "river_network_version_id": "rnv_a",
            "cycle_time": "2026-09-02T12:00:00Z",
            "updated_at": "2026-09-02T13:00:00Z",
        }
    ]
    _TILE_ROW = {
        "tile": b"pbf-bytes",
        "source_identity_count": 1,
        "source_feature_count": 1,
        "feature_count": 1,
        "coordinate_count": 2,
        "feature_coordinate_overflow_count": 0,
        "coordinate_dimension_overflow_count": 0,
        "invalid_property_count": 0,
        "invalid_properties": None,
    }

    def __init__(self, digest_rows: list[dict[str, Any]] | None = None) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.tile_params: list[dict[str, Any]] = []
        self.digest_params: list[dict[str, Any]] = []
        # Per instance, never by mutating `_DIGEST_ROWS`: the class attribute is
        # shared by every other case in this file.
        self.digest_rows = self._DIGEST_ROWS if digest_rows is None else digest_rows

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        if "ST_AsMVT" in str(statement):
            self.tile_params.append(dict(params or {}))
            return _TileResult([dict(self._TILE_ROW)])
        # The only other statement either national route issues is
        # `national_discharge_source_version`'s digest; recording its binds is
        # what lets a case assert the route narrowed it to the requested
        # identity (the tile binds alone cannot see that call at all).
        self.digest_params.append(dict(params or {}))
        return _TileResult([dict(row) for row in self.digest_rows])

    def get_bind(self) -> Any:
        return self.bind


class _ExplodingSession:
    """Any use at all is a failure: validation must precede every statement."""

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a validation failure must not reach the database")

    def get_bind(self) -> Any:
        raise AssertionError("a validation failure must not reach the database")


def _national_identity_url(
    source: str,
    cycle: str,
    valid_time: str = "2026-09-03T00:00:00Z",
    variable: str = "q_down",
    z: int = _NATIONAL_TILE_Z,
    x: int = _NATIONAL_TILE_X,
    y: int = _NATIONAL_TILE_Y,
) -> str:
    return f"{_NATIONAL_ROUTE_PREFIX}/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"


def _request_national_identity_tile(
    url: str,
    session: Any,
    monkeypatch: Any,
    tmp_path: Any,
) -> tuple[Any, list[TileInput]]:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setenv("NHMS_MVT_FILE_CACHE_DIR", str(tmp_path))
    captured: list[TileInput] = []

    def fake_read(_session: object, tile: TileInput) -> TileResponse | None:
        captured.append(tile)
        return None

    def fake_build(_session: object, tile: TileInput, data: bytes) -> TileResponse:
        return TileResponse(
            data=data,
            checksum="checksum",
            etag='W/"etag"',
            cache_key=cache_key(tile),
            cache_status="miss",
            layer_id=tile.layer_id,
        )

    monkeypatch.setattr(hydro_display, "read_cached_tile_response", fake_read)
    monkeypatch.setattr(hydro_display, "build_raw_tile_response", fake_build)

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(url)
    finally:
        app.dependency_overrides.clear()
    return response, captured


def test_national_identity_route_collapses_time_spellings_onto_one_bind_and_one_cache_key(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """`.000Z`, `+00:00`, `+08:00`, `-08:00` and `Z` are one instant, one `:cycle` bind, one cache entry.

    `+08:00` is here because the RFC3339 shape gate (#2007 F4) must not narrow
    the accepted spellings to UTC: `2026-09-02T20:00:00+08:00` is the SAME
    instant as the other three and must collapse onto the same canonical
    `2026-09-02T12:00:00Z` bind and cache key, not a fifth one.

    `2026-09-02T04:00:00-08:00` is the same instant again, and it is the ONLY
    accepted NEGATIVE offset anywhere in this file. Without it, narrowing
    `_RFC3339_INSTANT_RE`'s offset alternative from `[+-]` to `\\+` stays green:
    the only other negative-offset spelling in the suite is the year-9999
    reject-set case, which is rejected for RANGE, not shape, and would simply
    start being rejected one layer earlier -- silently taking the `OverflowError`
    guard's last discriminating oracle with it.
    """
    spellings = (
        "2026-09-02T12:00:00.000Z",
        "2026-09-02T12:00:00+00:00",
        "2026-09-02T20:00:00+08:00",
        "2026-09-02T04:00:00-08:00",
        "2026-09-02T12:00:00Z",
    )
    keys: list[str] = []
    binds: list[Any] = []
    captured: list[TileInput] = []
    for index, spelling in enumerate(spellings):
        session = _NationalRouteSession()
        response, captured = _request_national_identity_tile(
            _national_identity_url("gfs", quote(spelling, safe="")),
            session,
            monkeypatch,
            tmp_path / f"cache-{index}",
        )
        assert response.status_code == 200, response.text
        assert response.content == b"pbf-bytes"
        # The single-flight re-reads the cache inside the lock, so one request
        # offers the same TileInput twice; what matters is that it is the same.
        assert captured and len({cache_key(tile) for tile in captured}) == 1
        keys.append(cache_key(captured[0]))
        assert len(session.tile_params) == 1
        binds.append(session.tile_params[0])

    assert len(set(keys)) == 1, keys
    for bound in binds:
        assert bound["source"] == "gfs"
        assert bound["cycle"] == _NATIONAL_CYCLE
        assert bound["valid_time"] == _NATIONAL_VALID_TIME
    # Vacuity guard: the canonical cycle really is inside the cache identity.
    assert ":gfs:2026-09-02T12:00:00Z:" in captured[0].source_version


def test_national_identity_route_gives_two_identities_two_cache_keys(monkeypatch: Any, tmp_path: Any) -> None:
    """EVERY dimension `_national_source_cycle_tile_input` puts in the identity separates the cache.

    `source` and `cycle` ride in `source_version`; `valid_time` and `z`/`x`/`y`
    are fields of the `TileInput` itself, and the whole point of the route is
    that two requests that differ in ANY of them are two cache entries. The
    file cache has no TTL, so a dimension that silently drops out of the key
    serves the second requester the first requester's bytes forever.

    The x/y values are chosen so the two SLOT-SWAP mutations collide, which a
    pairwise `!=` between an arbitrary pair would not catch: against the base
    `(z=4, x=13, y=6)`, `y=y` → `y=x` makes the `y=13` case the base's twin,
    `x=x` → `x=y` makes the `x=6` case the base's twin, and `z=z` → `z=x` makes
    the `z=5` case the base's twin. Hence the assertion is on the SIZE of the
    distinct-key set: one collision in one dimension is one missing key.
    """
    urls = (
        _national_identity_url("gfs", "2026-09-02T12:00:00Z"),
        _national_identity_url("ifs", "2026-09-02T12:00:00Z"),
        _national_identity_url("gfs", "2026-09-02T00:00:00Z"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time="2026-09-03T06:00:00Z"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=5),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", x=6),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", y=13),
    )
    keys: list[str] = []
    for index, url in enumerate(urls):
        response, captured = _request_national_identity_tile(
            url,
            _NationalRouteSession(),
            monkeypatch,
            tmp_path / f"cache-{index}",
        )
        assert response.status_code == 200, response.text
        keys.append(cache_key(captured[0]))

    assert len(set(keys)) == len(urls), keys


@pytest.mark.parametrize(
    "url",
    [
        _national_identity_url("ERA5", "2026-09-02T12:00:00Z"),
        _national_identity_url("best", "2026-09-02T12:00:00Z"),
        _national_identity_url("gfs", "not-an-instant"),
        _national_identity_url("gfs", quote("2026-09-02T12:00:00.500Z", safe="")),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("2026-09-03T00:00:00.500Z", safe="")),
        # Spellings `cycle: datetime` coerces on its own, all of which reached
        # the tile SQL with a 200 before the RFC3339 shape gate: a bare Unix
        # epoch (which silently became 2025-09-02T12:00:00Z), an offset-less
        # local-looking instant, and a space-separated one.
        _national_identity_url("gfs", "1756814400"),
        _national_identity_url("gfs", "2026-09-02T12:00:00"),
        _national_identity_url("gfs", quote("2026-09-02 12:00:00", safe="")),
        # The same three in the `valid_time` position: the gate is on BOTH
        # instants, and a `cycle`-only gate would leave half the route lax.
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time="1756814400"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time="2026-09-03T00:00:00"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("2026-09-03 00:00:00", safe="")),
        # Well-formed RFC3339 that leaves `datetime`'s range once shifted to
        # UTC: the shape gate passes it and `astimezone` raised `OverflowError`,
        # i.e. an HTTP 500 from a public URL. It is a bad request, so it is 422.
        _national_identity_url("gfs", quote("9999-12-31T23:59:59-08:00", safe="")),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("9999-12-31T23:59:59-08:00", safe="")),
        # The LOWER bound in both positions too -- task 3.5 says "both extreme
        # instants", and `datetime.min` overflows on a POSITIVE offset, which the
        # year-9999 pair cannot reach. `quote(..., safe="")` is mandatory here:
        # an unescaped `+08:00` decodes as a space and would test a different,
        # shape-invalid input.
        _national_identity_url("gfs", quote("0001-01-01T00:00:00+08:00", safe="")),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", valid_time=quote("0001-01-01T00:00:00+08:00", safe="")),
        # `variable` is a path segment on this route too, and the route body's
        # comment claims a bad one costs no SQL. Only the SUPPORTED-set check has
        # a distinct oracle: `SUPPORTED_HYDRO_MVT_VARIABLES == ("q_down",)`, and
        # `q_down` satisfies `SAFE_TILE_IDENTIFIER_RE`, so every shape-invalid
        # spelling is also unsupported and `validate_identifier(variable, ...)`
        # can never be the layer that rejects. Both spellings are pinned anyway
        # because both are client-visible; the malformed one is subsumed.
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", variable="q_up"),
        _national_identity_url("gfs", "2026-09-02T12:00:00Z", variable=quote("q down", safe="")),
    ],
)
def test_national_identity_route_rejects_a_bad_identity_before_running_any_sql(url: str) -> None:
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(url)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text
    # One rejection contract for the whole route, whichever layer rejects:
    # FastAPI's own path validation (`source`, and the RFC3339 shape gate) and
    # the route body's `ApiError` (sub-second, out-of-UTC-range) must be
    # indistinguishable to a client.
    assert response.json()["error"]["code"] == "VALIDATION_ERROR", response.text


@pytest.mark.parametrize(
    ("case", "url"),
    [
        # z above MVT_MAX_ZOOM (14).
        ("z-too-large", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=15, x=0, y=0)),
        # z below 0. FastAPI parses `-1` as an int path param, so this really
        # does reach `validate_xyz` rather than failing to route.
        ("z-negative", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=-1, x=0, y=0)),
        # In-range z, x/y outside that zoom's 2^z matrix (z=4 -> 0..15).
        ("x-out-of-matrix", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=4, x=16, y=6)),
        ("y-out-of-matrix", _national_identity_url("gfs", "2026-09-02T12:00:00Z", z=4, x=13, y=16)),
    ],
)
def test_national_identity_route_rejects_bad_tile_coordinates_before_running_any_sql(
    case: str, url: str
) -> None:
    """`validate_xyz(z, x, y)` on the new route, which nothing pinned before.

    The route's `z`/`x`/`y` are plain `int` path params: the `maximum: 14` /
    `16383` in the runtime OpenAPI comes from
    `apps/api/openapi_patching.py::_patch_mvt_tile_openapi`, which rewrites the
    DOCUMENT and installs no validator. So `validate_xyz` is the only thing
    between a bad coordinate and the tile SQL, and deleting that one line from
    the route left the whole suite green -- `grep -rn "validate_xyz" tests/`
    matched nothing repo-wide.

    Under the deletion the request reaches `national_discharge_source_version`,
    `_ExplodingSession` raises, and the response becomes a 500: both the status
    and the code below move.

    The code is `TILE_XYZ_INVALID`, NOT the `VALIDATION_ERROR` every other
    rejection on this route renders. That is the pre-existing contract of
    `services/tiles/mvt.py::validate_xyz`, shared with the four sibling tile
    routes, and this route joins it rather than inventing a fifth spelling.
    """
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(url)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, f"{case}: {response.text}"
    assert response.json()["error"]["code"] == "TILE_XYZ_INVALID", f"{case}: {response.text}"


def _legacy_national_url(valid_time: str = "2026-09-03T00:00:00Z") -> str:
    return (
        f"{_NATIONAL_ROUTE_PREFIX}/q_down/{valid_time}"
        f"/{_NATIONAL_TILE_Z}/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"
    )


@pytest.mark.parametrize(
    ("route_name", "url", "expected_identity_binds"),
    [
        (
            "identity",
            _national_identity_url("gfs", "2026-09-02T12:00:00Z"),
            {"source": "gfs", "cycle": _NATIONAL_CYCLE},
        ),
        ("legacy", _legacy_national_url(), {"source": None, "cycle": None}),
    ],
)
def test_every_national_tile_sql_bind_is_supplied_by_the_route_that_executes_it(
    route_name: str, url: str, expected_identity_binds: dict[str, Any], monkeypatch: Any, tmp_path: Any
) -> None:
    """No bind in the national tile SQL may be missing from a call site's params.

    `text()` raises `StatementError: A value is required for bind parameter
    'source'` at execution time, so a bind added to the SQL without a matching
    param is a RUNTIME failure that no fake-session test sees: this file's fake
    session ignores its params entirely, and the real-DB suites that would catch
    it are opt-in. That is exactly how #2007's first pass left four cases in
    `test_river_ts_read_path_surrogate_keys_integration.py` broken. This case
    compares the two sets directly instead of trusting a human to remember.
    """
    declared = set(text(postgis_tile_sql("hydro-national"))._bindparams)
    # Non-vacuity: an empty or truncated `declared` would make the subset check
    # below pass for any call site at all.
    assert {"source", "cycle", "variable", "valid_time", "z", "x", "y"} <= declared

    session = _NationalRouteSession()
    response, _captured = _request_national_identity_tile(url, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    assert len(session.tile_params) == 1
    supplied = set(session.tile_params[0])
    assert declared - supplied == set(), f"{route_name} route omits binds the SQL declares"
    # ...and the VALUES, not just the key set. A key-set-only check is satisfied
    # by any value at all, so the legacy route binding `source="gfs"` -- which
    # would silently make the source-less alias filter on one source -- passed
    # here, passed
    # `test_each_national_route_hands_the_digest_helper_its_own_identity` (that
    # one asserts the DIGEST binds, a different call), and passed every
    # integration case, because every one of them seeds `_SOURCE_ID = "gfs"`.
    # The behavioral half is
    # `tests/test_mvt_national_identity_probe_integration.py`
    # ::test_national_identity_tile_matches_an_uppercase_source_id_from_a_lowercase_path,
    # where the legacy route must serve an `IFS`-only instant.
    identity_binds = {name: session.tile_params[0][name] for name in expected_identity_binds}
    assert identity_binds == expected_identity_binds, route_name


@pytest.mark.parametrize(
    ("route_name", "url", "expected_digest_params"),
    [
        (
            "identity",
            _national_identity_url("gfs", "2026-09-02T12:00:00Z"),
            {"source": "gfs", "cycle": _NATIONAL_CYCLE, "valid_time": _NATIONAL_VALID_TIME},
        ),
        (
            "legacy",
            _legacy_national_url(),
            {"source": None, "cycle": None, "valid_time": _NATIONAL_VALID_TIME},
        ),
    ],
)
def test_each_national_route_hands_the_digest_helper_its_own_identity(
    route_name: str,
    url: str,
    expected_digest_params: dict[str, Any],
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    """The identity route must narrow the digest to `(source, cycle)`; the legacy route must not.

    Freshness, not separation, is what this protects, and that is why nothing
    else catches it: `source_version` embeds the literal source and cycle text,
    so two identities keep two cache keys even when the route drops the kwargs.
    Rebinding the helper to a wrapper that swallows them left this file plus
    `test_api_contract`, `test_openapi_drift` and `test_display_publish_status_only`
    entirely green. What silently breaks is the other half: a RE-RUN of a
    non-latest `(source, cycle)` stops rotating the digest, so the cache key
    does not move, and the tile file cache has no TTL — the stale tile is served
    until something else evicts it.

    `test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one`
    proves the helper honours the arguments; this proves the routes pass them.
    """
    session = _NationalRouteSession()
    response, _captured = _request_national_identity_tile(url, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    # Non-vacuity: the digest really was computed once for this request. An
    # empty list would make the equality below unreachable, and a longer one
    # would mean a third statement now lands on this branch.
    assert len(session.digest_params) == 1, f"{route_name} route: {session.digest_params}"
    assert session.digest_params[0] == expected_digest_params, route_name


def test_national_identity_cache_key_moves_when_the_identity_digest_moves(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The digest must REACH the cache key, which is the freshness half of #2007.

    `test_each_national_route_hands_the_digest_helper_its_own_identity` proves
    the route passes `(source, cycle)` down, and
    `test_national_digest_narrows_to_the_requested_identity_and_stays_null_without_one`
    proves the helper uses them -- but neither looks at what the returned digest
    does next. Dropping `:{source_digest}` from `_national_source_cycle_tile_input`'s
    `source_version` leaves both of them green and every identity still gets its
    own cache key (the literal source and cycle text are in there too); what
    breaks is exactly the case the narrowing exists for: a RE-RUN of the SAME
    `(source, cycle)` no longer rotates the key, and the tile file cache has no
    TTL, so the stale tile is served until something else evicts it.

    Same identity, same URL, two different sets of ranked runs -> two keys.
    """
    rerun_rows = [{**_NationalRouteSession._DIGEST_ROWS[0], "run_id": "run_b"}]
    # Non-vacuity: the two digests really do differ, so a difference downstream
    # can be attributed to the digest rather than to anything else.
    assert national_discharge_source_version(_Session(_NationalRouteSession._DIGEST_ROWS)) != (
        national_discharge_source_version(_Session(rerun_rows))
    )

    url = _national_identity_url("gfs", "2026-09-02T12:00:00Z")
    keys: list[str] = []
    for index, rows in enumerate((None, rerun_rows)):
        _response, captured = _request_national_identity_tile(
            url, _NationalRouteSession(rows), monkeypatch, tmp_path / f"cache-{index}"
        )
        assert _response.status_code == 200, _response.text
        keys.append(cache_key(captured[0]))

    assert len(set(keys)) == 2, keys


def test_legacy_national_route_keeps_accepting_the_instant_spellings_it_always_did(
    monkeypatch: Any, tmp_path: Any
) -> None:
    """The RFC3339 shape gate is on the NEW route only, and that is load-bearing.

    `Rfc3339Instant` is deliberately not applied to the legacy 5-segment alias:
    `valid_time: datetime` there has always accepted offset-less and
    space-separated spellings, and clients are already sending them. Annotating
    the alias with `Rfc3339Instant` would turn those into 422s -- a silent
    break of the very route this change promises to leave alone -- and nothing
    in the repo noticed, because every legacy case in every suite happens to
    spell its instant `...Z`.

    This is a regression pin on the alias's pre-existing accept-set, not an
    endorsement of lax parsing; the new canonical route is where the shape gate
    lives.
    """
    session = _NationalRouteSession()
    response, captured = _request_national_identity_tile(
        _legacy_national_url(valid_time="2026-09-03T00:00:00"), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert len(session.tile_params) == 1
    # It lands on the same instant the `...Z` spelling does, so this pins the
    # accept-set without also blessing a second cache identity for it.
    assert captured[0].valid_time == "2026-09-03T00:00:00Z"


def _recording_digest_calls(monkeypatch: Any) -> list[dict[str, Any]]:
    """Swap `national_discharge_source_version` for a recorder of its kwargs."""
    recorded: list[dict[str, Any]] = []

    def _recording_digest(_session: Any, **kwargs: Any) -> str:
        recorded.append(kwargs)
        return "national-hydro-digest"

    monkeypatch.setattr(hydro_display, "national_discharge_source_version", _recording_digest)
    return recorded


def test_layer_catalog_digests_the_identity_it_advertises(monkeypatch: Any) -> None:
    """`GET /api/v1/layers` must digest `(default_source, default_cycle)`, nothing wider.

    The OPPOSITE of what this file pinned before #2009. Task 3.1 kept #2007's
    catalog call argument-free *because the catalog change is this issue's*, and
    named the hole verbatim: "一个非最新 `(source, cycle)` 身份的 re-run 不改变 digest,
    `cache_key` 不变而 tile 已陈旧——该洞正是因为本 issue 让旧身份可寻址才被打开". This is
    that issue, and the catalog now advertises `(default_source, default_cycle)`,
    so the digest must observe exactly those runs.

    Why the argument-free form is wrong here and not merely wider: it keeps one
    `rn = 1` row per network across ALL sources and cycles, so in the normal
    propagation state (some networks already on the next cycle) it observes no run
    of the advertised identity at all, and an `ifs` newest cycle blinds it to `gfs`
    entirely. A corrective re-run of the advertised identity would then leave
    `metadata.version` / `cache_version` unchanged, so the frontend's cache token
    and MapLibre source key would not move and the browser would keep the
    superseded tiles. Over-inclusion (an `ifs` landing rotating the `gfs` entry)
    goes away as a side effect.

    `_default_layer_catalog` runs for real here -- stubbing it away is what let the
    previous version of this test pass while asserting the wrong call shape.
    `tests/test_api_contract.py` still passes `national_hydro_source_version` as a
    literal and therefore never reaches the helper.
    """

    class _ValidTimes:
        valid_times = ["2026-09-02T12:00:00Z", "2026-09-02T15:00:00Z"]
        limit = 24
        observed_count = 2
        truncated = False

    recorded = _recording_digest_calls(monkeypatch)
    monkeypatch.setattr(
        hydro_display,
        "national_discharge_cycles",
        lambda _session, **_kwargs: {
            "source": "gfs",
            "cycles": [
                {
                    "cycle_time": "2026-09-02T12:00:00Z",
                    "valid_time_start": "2026-09-02T12:00:00Z",
                    "valid_time_end": "2026-09-02T15:00:00Z",
                }
            ],
            "default_cycle": "2026-09-02T12:00:00Z",
        },
    )
    monkeypatch.setattr(
        hydro_display, "national_discharge_valid_times", lambda _session, **_kwargs: _ValidTimes()
    )
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_1"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(
        hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a")
    )
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display, "_mvt_live_postgis_enabled", lambda _s: False)
    # `display_catalog_cached` is a process-wide TTL cache; without this the
    # loader may never run and `recorded` would be empty for the wrong reason.
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    # Exactly one call: `list_layers` no longer digests separately, so a leftover
    # argument-free call there would show up here as a second entry.
    assert len(recorded) == 1, recorded
    assert recorded[0] == {"source": "gfs", "cycle": datetime(2026, 9, 2, 12, tzinfo=UTC)}
    # #2031: identity only. Both TILE routes now bind their instant, and the
    # catalog has none -- it advertises a cycle, not a frame. Binding one here
    # would clamp the digest to a single instant of the timeline and leave the
    # entry's `metadata.version` describing whichever instant happened to be
    # picked. Subsumed by the equality above; spelled out because it is the
    # assertion the change's contract names.
    assert "valid_time" not in recorded[0], recorded[0]
    # The digest is scoped to the identity the SAME response advertises.
    assert _entry(response.json()["data"], "discharge")["metadata"]["default_cycle"] == (
        "2026-09-02T12:00:00Z"
    )


@pytest.mark.parametrize(
    ("advertised_default_cycle", "advertised_valid_times"),
    [
        pytest.param(None, [], id="intersection-already-empty-at-the-cycles-query"),
        pytest.param(
            "2026-09-02T12:00:00Z", [], id="intersection-empties-between-the-two-snapshots"
        ),
    ],
)
def test_layer_catalog_falls_back_to_the_argument_free_digest_when_no_cycle_is_advertised(
    monkeypatch: Any, advertised_default_cycle: str | None, advertised_valid_times: list[str]
) -> None:
    """With nothing addressable advertised, the digest must take NO identity kwargs.

    The other half of `test_layer_catalog_digests_the_identity_it_advertises`,
    which only pins the happy identity and stays green if the null branch is
    dropped. What makes the null branch worth its own pin is that dropping it
    fails QUIETLY. `national_discharge_source_version`'s cycle predicate is a
    null passthrough -- `CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time =
    :cycle` (`services/tiles/mvt.py`) -- so calling it with
    `source=NATIONAL_DISCHARGE_DEFAULT_SOURCE, cycle=None` raises nothing and
    selects nothing empty; it silently degenerates to source-only narrowing and
    returns a perfectly plausible digest of the latest `gfs` run per network.
    That is the wrong question for an entry that advertises no identity at all:
    the ranking it should reflect is every network's overall latest run, the one
    the argument-free form answers. Scoped to `gfs`, the digest stops observing
    the rest of the pipeline -- an `ifs` cycle landing, or changing which
    networks are covered, no longer moves `metadata.version`, and the frontend
    derives its cache token and MapLibre source key from exactly that string.
    `default_cycle = null` advertises nothing, so the honest input is every
    ranked run, i.e. the argument-free call.

    The second parameter is the ordering pin (invariant-matrix decision 10):
    `national_discharge_cycles` DOES hand back a cycle, and the emptiness only
    shows up in `national_discharge_valid_times`, which is a real race -- the two
    queries take separate `read committed` snapshots. `_default_layer_catalog`
    forces `default_cycle`/`default_cycle_instant` back to `None` on an empty
    timeline, and the digest must be computed AFTER that forcing. Hoisting the
    digest above it -- a plausible "compute the identity once, up top" cleanup --
    leaves this case digesting a cycle the response then refuses to advertise.
    """

    recorded = _recording_digest_calls(monkeypatch)
    monkeypatch.setattr(
        hydro_display,
        "national_discharge_cycles",
        lambda _session, **_kwargs: {
            "source": "gfs",
            "cycles": [],
            "default_cycle": advertised_default_cycle,
        },
    )
    monkeypatch.setattr(
        hydro_display,
        "national_discharge_valid_times",
        lambda _session, **_kwargs: SimpleNamespace(
            valid_times=advertised_valid_times,
            limit=24,
            observed_count=len(advertised_valid_times),
            truncated=False,
        ),
    )
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_1"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(
        hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a")
    )
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display, "_mvt_live_postgis_enabled", lambda _s: False)
    # `display_catalog_cached` is a process-wide TTL cache; without this the
    # loader may never run and `recorded` would be empty for the wrong reason.
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert len(recorded) == 1, recorded
    assert recorded[0] == {}
    # Non-vacuity: the digest is argument-free BECAUSE the entry ended up
    # advertising nothing. `(C, [])` is the forbidden pair, so an empty timeline
    # next to a non-null `default_cycle` would mean the forcing never ran and the
    # argument-free digest above was reached for some other reason.
    discharge = _entry(response.json()["data"], "discharge")
    assert discharge["metadata"]["default_cycle"] is None
    assert discharge["metadata"]["valid_times"] == []



def test_legacy_national_tile_route_digest_binds_the_instant_and_no_identity(monkeypatch: Any) -> None:
    """The 5-segment alias advertises no identity, so its digest must not narrow on one.

    Companion to `test_layer_catalog_digests_the_identity_it_advertises`: the
    catalog moved, this call site did not. Narrowing on `(source, cycle)` would
    change the alias\'s run selection, for a route whose whole contract is
    "unchanged run selection, unchanged bytes".

    `valid_time` is the one exception and it is not a narrowing of the identity
    (#2031): the alias\'s own tile SQL already clamps candidate runs to the
    instant\'s coverage window, so the digest passing the same instant makes the
    cache key describe the run the route actually paints. Its bytes and its
    200/424 verdict are untouched — only the key rotates, once, for instants
    outside the overall-latest run\'s window.
    """
    recorded = _recording_digest_calls(monkeypatch)
    monkeypatch.setattr(
        hydro_display,
        "_cached_or_generated_mvt_response",
        lambda *_a, **_k: hydro_display.Response(
            content=b"pbf", media_type=hydro_display.MVT_MEDIA_TYPE
        ),
    )

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(_legacy_national_url())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert len(recorded) == 1, recorded
    # Exactly the instant, and nothing else: `source`/`cycle` absent is what
    # keeps the alias source-less.
    assert recorded[0] == {"valid_time": datetime(2026, 9, 3, tzinfo=UTC)}


def test_runtime_openapi_documents_the_national_identity_tile_route() -> None:
    """Without the `mvt_paths` entry the runtime schema and the hand-written yaml
    would be consistently WRONG, so the equality drift test could not catch it."""
    operation = main.create_app().openapi()["paths"][
        "/api/v1/tiles/hydro-national/{source}/{cycle}/{variable}/{valid_time}/{z}/{x}/{y}.pbf"
    ]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert operation["responses"]["424"] == {"$ref": "#/components/responses/MvtLivePostgisUnavailable"}
    assert parameters["variable"]["schema"]["enum"] == ["q_down"]
    assert parameters["source"]["schema"]["enum"] == ["gfs", "ifs"]
    assert parameters["z"]["schema"]["maximum"] == 14
    assert parameters["x"]["schema"]["maximum"] == 16383
    assert parameters["y"]["schema"]["maximum"] == 16383


# ---------------------------------------------------------------------------
# #2009: the national discharge cycles catalog and its per-cycle valid times.
#
# The fixture below answers the two statements `_national_discharge_coverage_rows`
# runs and filters the coverage rows by the BOUND VALUES, never by the SQL text.
# That is deliberate: a fake that ignored `params` would keep every "the argument
# reaches the query" claim green while the call site dropped it.
# ---------------------------------------------------------------------------

_CYCLE = datetime(2026, 9, 2, 12, tzinfo=UTC)
_PREVIOUS_CYCLE = datetime(2026, 9, 2, 6, tzinfo=UTC)
_INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# `national_discharge_cycles` bounds its scan at `now() - LOOKBACK`, but the cases
# above and below are about intersection, ordering and spelling, and they express
# their expectations as FIXED instants. Left alone they would pass today and start
# failing the day wall-clock time walks past `_CYCLE + 12 days` -- a suite that
# rots on a calendar, not on a code change. So the window is widened for the whole
# module and the three cases that are actually ABOUT the bound put the real value
# back. `national_discharge_cycles` reads the module global on every call, so this
# reaches the route/catalog tests through `TestClient` as well.
_REAL_CYCLE_LOOKBACK_DAYS = mvt_module.NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS


@pytest.fixture(autouse=True)
def _keep_fixed_instant_fixtures_inside_the_cycle_lookback(monkeypatch: Any) -> None:
    monkeypatch.setattr(mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", 100_000)


def _cycle_days_ago(days: int) -> datetime:
    """A cycle `days` before now, aligned to the hour.

    Relative, because these are the cases the lookback bound actually moves, and
    hour-aligned because `_format_time` spells seconds and the rectangle check
    divides the coverage window by 3600.
    """
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(days=days)


def _coverage_row(
    *,
    network: str,
    cycle: datetime,
    start: datetime,
    end: datetime,
    source: str = "gfs",
    segment_count: int = 2,
    run_id: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """One `hydro.run_display_coverage` row that proves a complete hourly rectangle."""
    lead_count = int((end - start).total_seconds()) // 3600 + 1
    row: dict[str, Any] = {
        "run_id": run_id or f"run-{network}-{cycle:%Y%m%d%H}",
        "basin_version_id": f"bv-{network}",
        "river_network_version_id": network,
        "cycle_time": cycle,
        "source_id": source,
        "segment_count": segment_count,
        "river_sample_count": segment_count * lead_count,
        "river_valid_time_start": start,
        "river_valid_time_end": end,
        "min_lead_time_hours": 0,
        "max_lead_time_hours": lead_count - 1,
    }
    row.update(overrides)
    return row


class _NationalDiscoverySession:
    """Answers the active-network query and the identity-bound coverage query."""

    def __init__(self, rows: list[dict[str, Any]], *, active_networks: list[str] | None = None) -> None:
        self.rows = rows
        self.active_networks = (
            active_networks
            if active_networks is not None
            else sorted({row["river_network_version_id"] for row in rows})
        )
        self.executions: list[tuple[str, Any]] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        sql = str(statement)
        self.executions.append((sql, params))
        if "core.model_instance mi" in sql and "hydro.hydro_run" not in sql:
            return _Rows([{"river_network_version_id": network} for network in self.active_networks])
        if "hydro.run_display_coverage" not in sql:
            # Anything else (`display_ready_run`, `_run_row`) finds nothing.
            return _Rows([])
        bound = params or {}
        source = bound.get("source")
        cycle = bound.get("cycle")
        since = bound.get("since")
        selected = []
        for row in self.rows:
            if source is not None and str(row["source_id"]).lower() != source:
                continue
            if cycle is not None and canonical_mvt_time(row["cycle_time"]) != canonical_mvt_time(cycle):
                continue
            # `h.cycle_time >= :since` in SQL, NULL semantics included: `NULL >= x`
            # is NULL, so a run with no cycle is dropped by a bound `:since` rather
            # than kept. Without this branch the lookback predicate could be deleted
            # from the statement and no behavioural test would notice.
            if since is not None and (row["cycle_time"] is None or row["cycle_time"] < since):
                continue
            # The real statement does not select `source_id`; neither does this.
            selected.append({key: value for key, value in row.items() if key != "source_id"})
        return _Rows(selected)

    def get_bind(self) -> Any:
        return self.bind


def _full_coverage_rows(cycle: datetime, networks: tuple[str, ...] = ("rn-a", "rn-b", "rn-c")) -> list[dict[str, Any]]:
    return [
        _coverage_row(network=network, cycle=cycle, start=cycle, end=cycle + timedelta(hours=168))
        for network in networks
    ]


def test_national_cycles_intersection_excludes_a_partially_covered_cycle() -> None:
    """38-networks-have-A / 37-have-B, shrunk to three and two."""
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE) + _full_coverage_rows(_PREVIOUS_CYCLE, networks=("rn-a", "rn-b"))
    )

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == ["2026-09-02T12:00:00Z"]
    assert result["default_cycle"] == "2026-09-02T12:00:00Z"
    assert result["source"] == "gfs"


def test_national_cycles_are_sorted_newest_first_and_default_to_the_newest() -> None:
    cycles = [_CYCLE, _PREVIOUS_CYCLE, datetime(2026, 9, 2, 0, tzinfo=UTC)]
    rows: list[dict[str, Any]] = []
    # Interleaved on purpose: sorted() must do the work, not the row order.
    for cycle in (cycles[1], cycles[2], cycles[0]):
        rows.extend(_full_coverage_rows(cycle))
    session = _NationalDiscoverySession(rows)

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T06:00:00Z",
        "2026-09-02T00:00:00Z",
    ]
    assert result["default_cycle"] == result["cycles"][0]["cycle_time"]


def test_national_cycle_lookback_leaves_a_day_of_precip_mirror_margin() -> None:
    """`canonical-precip-copyback`: `oldest_listed_cycle - 24h >= display_watermark - retention_days`.

    With lookback `L` and the raw-retention run's `retention_days` `R` that sentence
    reduces to `L <= R - 1` -- an INEQUALITY, which is why this asserts one instead
    of matching the retention default. The same run prunes the canonical
    precipitation mirror and the precipitation PNG cache on that cutoff, so `R` is
    the right constant to pin against even though the discharge tiles' own
    timeseries lane is wider.

    Raising the DEPLOYED retention (`NODE27_RAW_RETENTION_DAYS` / `--retention-days`) is the
    copyback spec's own remedy, but this test does not observe it: what it pins is the SOURCE
    default `DEFAULT_RETENTION_DAYS` (`scripts/node27_raw_retention.py`), and lowering THAT
    below 13 is what goes red. The inequality is the load-bearing half -- round 1 asserted
    `== 14` under a name claiming a coupling it never checked, and that green test passed.
    """
    assert _REAL_CYCLE_LOOKBACK_DAYS <= DEFAULT_RETENTION_DAYS - 1
    assert _REAL_CYCLE_LOOKBACK_DAYS == 12


def test_national_cycles_list_only_cycles_inside_the_lookback_window(monkeypatch: Any) -> None:
    """Both cycles are covered by EVERY active network; only the recent one is listed."""
    monkeypatch.setattr(
        mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", _REAL_CYCLE_LOOKBACK_DAYS
    )
    recent = _cycle_days_ago(1)
    stale = _cycle_days_ago(30)
    session = _NationalDiscoverySession(_full_coverage_rows(recent) + _full_coverage_rows(stale))

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == [canonical_mvt_time(recent)]
    assert result["default_cycle"] == canonical_mvt_time(recent)
    # The fake filters on the BOUND value, so the behavioural assertions above stay
    # green if the predicate is deleted from the statement but the bind is left in
    # place. This pins the statement half of the same claim.
    coverage_sql = next(sql for sql, _ in session.executions if "hydro.run_display_coverage" in sql)
    assert "AND (CAST(:since AS timestamptz) IS NULL OR h.cycle_time >= :since)" in coverage_sql


def test_national_cycles_lookback_reads_the_module_constant(monkeypatch: Any) -> None:
    """The call site must consult `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`, not a literal.

    The other lookback cases put the REAL value back and use 20/30-day-old cycles,
    which are stale under any plausible literal too -- so hardcoding `days=14` at
    the `since=` call site -- the constant's value at that head -- left the whole
    suite green (measured `360 pass / 0 fail` at review round 2). This case moves
    the constant to 3, past the module's autouse widening, and puts one cycle at
    1 day and one at 5: both are inside any literal the window could be hardcoded
    to, so only a call site that really reads the constant drops the 5-day one.
    """
    monkeypatch.setattr(mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", 3)
    inside = _cycle_days_ago(1)
    outside = _cycle_days_ago(5)
    session = _NationalDiscoverySession(_full_coverage_rows(inside) + _full_coverage_rows(outside))

    result = national_discharge_cycles(session, source="gfs")

    assert [entry["cycle_time"] for entry in result["cycles"]] == [canonical_mvt_time(inside)]
    assert result["default_cycle"] == canonical_mvt_time(inside)


def test_national_cycles_are_empty_when_every_covered_cycle_predates_the_lookback(
    monkeypatch: Any,
) -> None:
    """An ingest stall longer than the window: fail-closed, not a stale advertisement."""
    monkeypatch.setattr(
        mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", _REAL_CYCLE_LOOKBACK_DAYS
    )
    session = _NationalDiscoverySession(
        _full_coverage_rows(_cycle_days_ago(20)) + _full_coverage_rows(_cycle_days_ago(30))
    )

    result = national_discharge_cycles(session, source="gfs")

    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_no_argument_national_valid_times_keep_a_network_whose_newest_run_predates_the_lookback(
    monkeypatch: Any,
) -> None:
    """The bound belongs to `cycles` alone; the no-argument branch stays unbounded.

    `rn-a`'s newest display-ready run is 20 days old and still covers the next few
    days. Master intersects it with the other two networks; binding the same
    `:since` here would drop `rn-a` from the intersection entirely and widen the
    advertised window to one `rn-a` cannot render.
    """
    monkeypatch.setattr(
        mvt_module, "NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS", _REAL_CYCLE_LOOKBACK_DAYS
    )
    stale = _cycle_days_ago(20)
    fresh = _cycle_days_ago(1)
    common_end = fresh + timedelta(days=3)
    session = _NationalDiscoverySession(
        [
            _coverage_row(network="rn-a", cycle=stale, start=stale, end=common_end),
            _coverage_row(network="rn-b", cycle=fresh, start=fresh, end=fresh + timedelta(days=6)),
            _coverage_row(network="rn-c", cycle=fresh, start=fresh, end=fresh + timedelta(days=6)),
        ]
    )

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times[0] == canonical_mvt_time(fresh)
    assert discovery.valid_times[-1] == canonical_mvt_time(common_end)
    assert discovery.observed_count == 73
    assert discovery.truncated is False


def test_national_cycles_fail_closed_when_a_network_activates_between_the_two_statements() -> None:
    """Equal cardinality, different membership -- the minimal fail-OPEN race.

    The denominator query and the coverage query are two statements with their own
    READ COMMITTED snapshots. Statement 1 sees the active set `{rn-b, rn-c1, rn-c2}`;
    `rn-a` is then activated and already holds a display-ready run for the cycle,
    so statement 2 (which re-evaluates `active_flag`) returns `{rn-a, rn-c1, rn-c2}`
    while `rn-b` never had that cycle at all. `3 == 3`, so a cardinality comparison
    lists a cycle the active network `rn-b` cannot render. The set comparison
    refuses it.
    """
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-c1", "rn-c2")),
        active_networks=["rn-b", "rn-c1", "rn-c2"],
    )

    result = national_discharge_cycles(session, source="gfs")

    # Non-vacuity: the two statements really do disagree at equal size.
    assert len(session.active_networks) == 3
    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_national_cycles_fail_closed_when_one_active_network_has_no_run_for_the_source() -> None:
    """The uncovered network contributes NO row, so only `core.model_instance` can see it."""
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")),
        active_networks=["rn-a", "rn-b", "rn-c"],
    )

    result = national_discharge_cycles(session, source="gfs")

    assert result["cycles"] == []
    assert result["default_cycle"] is None


def test_national_cycles_treat_a_zero_segment_run_as_uncovered() -> None:
    rows = _full_coverage_rows(_CYCLE)
    rows[-1] = _coverage_row(
        network="rn-c",
        cycle=_CYCLE,
        start=_CYCLE,
        end=_CYCLE + timedelta(hours=168),
        segment_count=0,
        river_sample_count=0,
    )
    session = _NationalDiscoverySession(rows)

    assert national_discharge_cycles(session, source="gfs")["cycles"] == []


def test_national_cycles_list_only_the_requested_source() -> None:
    rows = _full_coverage_rows(_CYCLE)
    rows.extend(
        _coverage_row(
            network=network,
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=168),
            source="IFS",
            run_id=f"run-ifs-{network}",
        )
        for network in ("rn-a", "rn-b", "rn-c")
    )
    session = _NationalDiscoverySession(rows)

    gfs = national_discharge_cycles(session, source="gfs")
    ifs = national_discharge_cycles(session, source="ifs")

    assert [entry["cycle_time"] for entry in gfs["cycles"]] == ["2026-09-02T12:00:00Z"]
    # `lower(h.source_id)`: production stores `IFS` upper-case.
    assert [entry["cycle_time"] for entry in ifs["cycles"]] == ["2026-09-02T06:00:00Z"]


def test_national_cycles_skip_a_cycle_whose_window_holds_no_stride_instant() -> None:
    """`run_display_coverage` is HOURLY, so a covered window can miss the 3-hour grid."""
    session = _NationalDiscoverySession(
        [
            _coverage_row(
                network=network,
                cycle=_CYCLE,
                start=_CYCLE + timedelta(hours=1),
                end=_CYCLE + timedelta(hours=2),
            )
            for network in ("rn-a", "rn-b")
        ]
    )

    assert national_discharge_cycles(session, source="gfs")["cycles"] == []


def test_national_cycles_and_valid_times_agree_on_every_listed_window() -> None:
    """Cross-endpoint: the cycles row's endpoints ARE the endpoints of the list."""
    rows = _full_coverage_rows(_CYCLE)
    rows[0] = _coverage_row(
        network="rn-a", cycle=_CYCLE, start=_CYCLE + timedelta(hours=6), end=_CYCLE + timedelta(hours=96)
    )
    rows.extend(_full_coverage_rows(_PREVIOUS_CYCLE))
    session = _NationalDiscoverySession(rows)

    listed = national_discharge_cycles(session, source="gfs")["cycles"]

    assert len(listed) == 2
    for entry in listed:
        discovery = national_discharge_valid_times(
            session, source="gfs", cycle=datetime.fromisoformat(entry["cycle_time"])
        )
        assert discovery.valid_times[0] == entry["valid_time_start"]
        assert discovery.valid_times[-1] == entry["valid_time_end"]
    assert listed[0]["valid_time_start"] == "2026-09-02T18:00:00Z"
    assert listed[0]["valid_time_end"] == "2026-09-06T12:00:00Z"


def test_national_per_cycle_valid_times_are_fifty_seven_three_hour_entries() -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert len(discovery.valid_times) == 57
    assert discovery.valid_times[0] == "2026-09-02T12:00:00Z"
    assert discovery.valid_times[-1] == "2026-09-09T12:00:00Z"
    assert discovery.observed_count == 57
    assert discovery.truncated is False
    instants = [datetime.fromisoformat(value) for value in discovery.valid_times]
    assert {later - earlier for earlier, later in zip(instants, instants[1:])} == {timedelta(hours=3)}


def test_national_per_cycle_valid_times_stop_at_the_earliest_coverage_end() -> None:
    rows = _full_coverage_rows(_CYCLE)
    rows[1] = _coverage_row(network="rn-b", cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=96))
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times[0] == "2026-09-02T12:00:00Z"
    assert discovery.valid_times[-1] == "2026-09-06T12:00:00Z"
    assert len(discovery.valid_times) == 33


def test_national_per_cycle_valid_times_start_at_the_latest_coverage_start() -> None:
    """Clamped below too: advertising an instant no basin can render is the bug."""
    rows = _full_coverage_rows(_CYCLE)
    rows[1] = _coverage_row(
        network="rn-b", cycle=_CYCLE, start=_CYCLE + timedelta(hours=6), end=_CYCLE + timedelta(hours=168)
    )
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times[0] == "2026-09-02T18:00:00Z"
    assert discovery.valid_times[-1] == "2026-09-09T12:00:00Z"
    assert len(discovery.valid_times) == 55


def test_national_per_cycle_valid_times_fail_closed_for_an_off_phase_coverage_grid() -> None:
    """`run_display_coverage` is an HOURLY grid, and this branch strides from the cycle.

    `rn-b` starts 90 minutes after the cycle, so its samples land at `:30` and it
    has NO sample at any `cycle + 3k h`. The rectangle check cannot see this --
    it is translation-invariant in the start instant, and this row is a perfectly
    complete 13-lead rectangle. The no-argument branch has carried the equivalent
    guarantee since before #2009 (`(common_end - start) % 3600`); moving the
    catalog's advertised list onto this branch must not lose it.
    """
    rows = [
        _coverage_row(network="rn-a", cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=12)),
        _coverage_row(
            network="rn-b",
            cycle=_CYCLE,
            start=_CYCLE + timedelta(minutes=90),
            end=_CYCLE + timedelta(hours=13, minutes=30),
        ),
    ]
    session = _NationalDiscoverySession(rows)

    assert national_discharge_valid_times(session, source="gfs", cycle=_CYCLE).valid_times == []
    # `national_discharge_cycles` computes its entries through the same helper, so
    # it inherits the guard: an unrenderable cycle is not listed either.
    assert national_discharge_cycles(session, source="gfs")["cycles"] == []


def test_national_per_cycle_valid_times_are_empty_for_a_cycle_outside_the_intersection() -> None:
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")),
        active_networks=["rn-a", "rn-b", "rn-c"],
    )

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times == []
    assert discovery.observed_count == 0


def test_national_per_cycle_valid_times_answer_for_the_requested_cycle() -> None:
    """Two cycles with DIFFERENT windows, so mixing them in changes the answer."""
    rows = _full_coverage_rows(_CYCLE)
    rows.extend(
        _coverage_row(
            network=network,
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=12),
        )
        for network in ("rn-a", "rn-b", "rn-c")
    )
    session = _NationalDiscoverySession(rows)

    older = national_discharge_valid_times(session, source="gfs", cycle=_PREVIOUS_CYCLE)

    assert older.valid_times[0] == "2026-09-02T06:00:00Z"
    assert older.valid_times[-1] == "2026-09-02T18:00:00Z"
    assert len(older.valid_times) == 5


def test_national_per_cycle_valid_times_keep_the_first_entries_when_truncated() -> None:
    """Unlike the no-argument branch, which keeps the TAIL: this list starts at the cycle."""
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE, limit=5)

    assert discovery.valid_times == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T15:00:00Z",
        "2026-09-02T18:00:00Z",
        "2026-09-02T21:00:00Z",
        "2026-09-03T00:00:00Z",
    ]
    assert discovery.observed_count == 57
    assert discovery.limit == 5
    assert discovery.truncated is True


def test_no_argument_national_valid_times_discard_an_older_malformed_cycle() -> None:
    """Selection BEFORE validation: one bad historical row must not blank the catalog."""
    rows = [
        _coverage_row(network="rn-a", cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=3)),
        _coverage_row(
            network="rn-a",
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=3),
            river_sample_count=3,  # not segment_count * lead_count -> not a rectangle
        ),
    ]
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T13:00:00Z",
        "2026-09-02T14:00:00Z",
        "2026-09-02T15:00:00Z",
    ]


def test_no_argument_national_valid_times_rank_a_null_cycle_first_like_the_tile_ctes() -> None:
    """PostgreSQL's `ORDER BY h.cycle_time DESC` implies NULLS FIRST.

    Pre-#2009 that ordering lived entirely in SQL, so a display-ready run with
    `cycle_time IS NULL` won `rn = 1` for its network. The two national tile CTEs
    still rank in raw SQL and are untouched by #2009, so ranking such a row LAST
    in Python would make the discovery endpoint advertise one run's window while
    the tile route paints a different run's geometry.
    """
    rows = [
        _coverage_row(
            network="rn-a",
            cycle=_CYCLE,
            start=_CYCLE,
            end=_CYCLE + timedelta(hours=3),
            run_id="run-a-null-cycle",
            cycle_time=None,
        ),
        _coverage_row(
            network="rn-a",
            cycle=_CYCLE,
            start=_CYCLE + timedelta(hours=10),
            end=_CYCLE + timedelta(hours=12),
            run_id="run-a-cycled",
        ),
    ]
    session = _NationalDiscoverySession(rows)

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T13:00:00Z",
        "2026-09-02T14:00:00Z",
        "2026-09-02T15:00:00Z",
    ]
    # The cycles list goes the OTHER way on purpose: a NULL cycle has no spelling
    # to put in `cycles[].cycle_time` or a tile URL's `{cycle}`, so it is skipped
    # and only the real cycle is advertised.
    listed = national_discharge_cycles(session, source="gfs")["cycles"]
    assert [entry["cycle_time"] for entry in listed] == ["2026-09-02T12:00:00Z"]


def test_national_valid_times_reject_half_an_identity() -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    with pytest.raises(ValueError):
        national_discharge_valid_times(session, source="gfs")
    with pytest.raises(ValueError):
        national_discharge_valid_times(session, cycle=_CYCLE)


def test_layer_source_refs_refuses_the_discharge_layer() -> None:
    """`layer_metadata` short-circuits discharge to `source_refs={}`; this is the backstop.

    A future refactor that wires discharge back through this helper would put
    `run_id` into the metadata version hash input again and split the runless and
    run-scoped catalogs' ETags. The entry assertion has existed since PR #602 with
    no test at all.
    """
    with pytest.raises(AssertionError):
        mvt_module._layer_source_refs(
            layer_id="discharge",
            run_id="run_1",
            source_version="v1",
            basin_version_id="bv_a",
            river_network_version_id="rnv_a",
        )


def test_national_discharge_metadata_advertises_exactly_one_identity() -> None:
    metadata = layer_metadata(
        "discharge",
        run_id="run_1",
        source_version="national-hydro-v1",
        valid_times=["2026-09-02T12:00:00Z"],
        national=True,
        default_cycle="2026-09-02T12:00:00Z",
    )

    assert metadata["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert metadata["url_template"] == metadata["tile_url_template"]
    assert metadata["required_placeholders"] == ["source", "cycle", "valid_time", "z", "x", "y"]
    assert "{run_id}" not in metadata["tile_url_template"]
    assert metadata["default_source"] == "gfs"
    assert metadata["default_cycle"] == "2026-09-02T12:00:00Z"
    assert metadata["cycles_url_template"] == "/api/v1/layers/discharge/cycles?source={source}"
    assert metadata["valid_times_url_template"] == (
        "/api/v1/layers/discharge/valid-times?source={source}&cycle={cycle}"
    )
    assert metadata["maplibre_source_layer"] == "hydro"
    assert "basin_id" in metadata["property_schema"]["required"]
    assert metadata["source_refs"] == {}


@pytest.mark.parametrize("field", ["default_source", "cycles_url_template", "valid_times_url_template"])
def test_national_discharge_metadata_version_hashes_every_identity_field(monkeypatch: Any, field: str) -> None:
    """Each of the four is in the `_stable_json_hash` input, not only in the payload.

    The version is the ETag input, so a contract field that the payload advertises
    and the hash ignores would let a client keep a cached catalog whose identity
    has changed underneath it.
    """
    kwargs: dict[str, Any] = {
        "source_version": "national-hydro-v1",
        "valid_times": ["2026-09-02T12:00:00Z"],
        "national": True,
        "default_cycle": "2026-09-02T12:00:00Z",
    }
    baseline = layer_metadata("discharge", **kwargs)["cache_version"]

    assert layer_metadata("discharge", **{**kwargs, "default_cycle": "2026-09-02T06:00:00Z"})[
        "cache_version"
    ] != baseline
    monkeypatch.setitem(mvt_module._NATIONAL_DISCHARGE_METADATA, field, "/moved")
    assert layer_metadata("discharge", **kwargs)["cache_version"] != baseline


def test_sibling_layer_metadata_versions_are_untouched_by_the_discharge_identity() -> None:
    """Frozen against the values master emits: the four new fields are discharge-only.

    Adding them unconditionally to the hash input would rotate every layer's
    `cache_version` and therefore every layer's metadata ETag, for a contract
    that only the discharge entry changed.
    """
    national_river = layer_metadata("river-network", source_version="generation-a", national=True)
    run_scoped_river = layer_metadata(
        "river-network",
        run_id="run_1",
        basin_version_id="bv_a",
        river_network_version_id="rnv_a",
        source_version="run-source-v1",
    )

    assert national_river["tile_url_template"] == "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf"
    assert national_river["required_placeholders"] == ["z", "x", "y"]
    assert national_river["cache_version"] == (
        "3781c3e391aa38d7dc3072c5f50ee9ec07396277c83e9d9aa95ec8e5b3e91679"
    )
    assert run_scoped_river["tile_url_template"] == (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"
    )
    assert run_scoped_river["required_placeholders"] == ["basin_version_id", "z", "x", "y"]
    assert run_scoped_river["cache_version"] == (
        "73a44576017f0956ff4e93f0d9b99e0e6315b32b2801b06cc0eb305b4a5ca733"
    )
    for metadata in (national_river, run_scoped_river):
        for field in ("default_source", "default_cycle", "cycles_url_template", "valid_times_url_template"):
            assert field not in metadata


def _national_catalog_app(
    monkeypatch: Any,
    session: Any,
    *,
    display_ready: dict[str, Any] | None = None,
) -> Any:
    """A `/api/v1/layers` app whose ONLY live query path is the national discovery one."""
    monkeypatch.setattr(
        hydro_display, "display_ready_run", lambda _session: display_ready or {"run_id": "run_latest"}
    )
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a"))
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(
        hydro_display, "national_discharge_source_version", lambda _s, **_k: "national-hydro-v1"
    )
    monkeypatch.setattr(hydro_display, "_mvt_live_postgis_enabled", lambda _s: False)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    return app


def _entry(items: list[dict[str, Any]], layer_id: str) -> dict[str, Any]:
    return next(item for item in items if item["layer_id"] == layer_id)


def test_layer_catalog_discharge_entry_is_byte_identical_runless_and_run_scoped(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _national_catalog_app(monkeypatch, session)
    # A DIFFERENT run than the latest one, so a `run_id`-dependent default cycle
    # would have to diverge somewhere.
    monkeypatch.setattr(
        hydro_display, "_require_display_ready", lambda _s, run_id: {"run_id": run_id, "status": "published"}
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            runless = client.get("/api/v1/layers")
            run_scoped = client.get("/api/v1/layers", params={"run_id": "run_other"})
    finally:
        app.dependency_overrides.clear()

    assert runless.status_code == 200, runless.text
    assert run_scoped.status_code == 200, run_scoped.text
    discharge = _entry(runless.json()["data"], "discharge")
    assert discharge == _entry(run_scoped.json()["data"], "discharge")

    metadata = discharge["metadata"]
    assert metadata["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert metadata["required_placeholders"] == ["source", "cycle", "valid_time", "z", "x", "y"]
    assert metadata["default_source"] == "gfs"
    assert metadata["default_cycle"] == "2026-09-02T12:00:00Z"
    assert len(metadata["valid_times"]) == 57
    assert metadata["source_refs"] == {}
    assert metadata["maplibre_source_layer"] == "hydro"
    assert "basin_id" in metadata["property_schema"]["required"]
    assert _INSTANT_RE.match(metadata["default_cycle"])
    assert all(_INSTANT_RE.match(instant) for instant in metadata["valid_times"])

    # Unchanged sibling: `river-network` keeps its two caller-shaped templates.
    assert _entry(runless.json()["data"], "river-network")["metadata"]["tile_url_template"] == (
        "/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf"
    )
    assert _entry(run_scoped.json()["data"], "river-network")["metadata"]["tile_url_template"] == (
        "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"
    )


def test_layer_catalog_keeps_the_discharge_entry_when_the_intersection_is_empty(monkeypatch: Any) -> None:
    """Runs exist, no cycle covers every network: an honest fail-closed entry, not a drop."""
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-b")),
        active_networks=["rn-a", "rn-b", "rn-c"],
    )
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    metadata = _entry(response.json()["data"], "discharge")["metadata"]
    assert metadata["default_cycle"] is None
    assert metadata["valid_times"] == []
    assert metadata["default_source"] == "gfs"


class _NetworkVanishesBetweenCallsSession(_NationalDiscoverySession):
    """`read committed`: the catalog's two coverage queries take two snapshots.

    The unbound query (`national_discharge_cycles`) still sees every network
    covered; the `cycle`-bound query that follows finds `rn-c` gone -- what a
    network being newly ACTIVATED without a display-ready run for that cycle, or
    a covered run's status being rewritten, looks like from here.
    """

    def execute(self, statement: Any, params: Any = None) -> _Rows:
        rows = super().execute(statement, params)
        if (params or {}).get("cycle") is None:
            return rows
        return _Rows([row for row in rows.all() if row["river_network_version_id"] != "rn-c"])


def test_layer_catalog_never_advertises_a_cycle_whose_timeline_came_back_empty(monkeypatch: Any) -> None:
    """`(default_cycle=C, valid_times=[])` is a state the contract forbids.

    The empty intersection has exactly one spelling -- `default_cycle` null AND
    `valid_times` empty -- so the frontend cannot read "there is a default cycle,
    it just has no timeline" and pin the map to a cycle nothing can render.
    """
    session = _NetworkVanishesBetweenCallsSession(_full_coverage_rows(_CYCLE))
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    metadata = _entry(response.json()["data"], "discharge")["metadata"]
    assert metadata["valid_times"] == []
    assert metadata["default_cycle"] is None
    # Still the fail-closed ENTRY, not a dropped layer (the other empty state).
    assert metadata["default_source"] == "gfs"


def test_layer_catalog_is_empty_when_no_run_is_display_ready(monkeypatch: Any) -> None:
    """The other empty state: no ghost discharge entry when nothing is renderable at all."""
    session = _NationalDiscoverySession([])
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: None)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


def test_layer_catalog_rejects_an_unknown_run_without_a_discharge_side_channel(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers", params={"run_id": "run_missing"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert "data" not in response.json()


def test_layer_catalog_advertises_the_list_the_valid_times_endpoint_serves(monkeypatch: Any) -> None:
    """One stride implementation: the catalog's list and the endpoint's must be identical.

    The fixture clamps the lower bound (one network starts six hours late), so a
    second, private stride computation in the catalog would have to reproduce the
    clamp as well to stay equal.
    """
    rows = _full_coverage_rows(_CYCLE)
    rows[0] = _coverage_row(
        network="rn-a", cycle=_CYCLE, start=_CYCLE + timedelta(hours=6), end=_CYCLE + timedelta(hours=96)
    )
    session = _NationalDiscoverySession(rows)
    app = _national_catalog_app(monkeypatch, session)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            catalog = client.get("/api/v1/layers")
            metadata = _entry(catalog.json()["data"], "discharge")["metadata"]
            endpoint = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": metadata["default_source"], "cycle": metadata["default_cycle"]},
            )
    finally:
        app.dependency_overrides.clear()

    assert endpoint.status_code == 200, endpoint.text
    assert metadata["valid_times"] == endpoint.json()["data"]["valid_times"]
    assert metadata["valid_times"][0] == "2026-09-02T18:00:00Z"
    assert metadata["valid_times"][-1] == "2026-09-06T12:00:00Z"
    # The advertised list is the per-cycle 3-hour stride, not the no-argument
    # branch's hourly union: falling back to the old list would keep the two
    # sides equal while advertising an identity nothing asked for.
    instants = [datetime.fromisoformat(value) for value in metadata["valid_times"]]
    assert {later - earlier for earlier, later in zip(instants, instants[1:])} == {timedelta(hours=3)}


def test_discharge_cycles_route_returns_the_intersection_in_the_pinned_spelling(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE) + _full_coverage_rows(_PREVIOUS_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers/discharge/cycles", params={"source": "gfs"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "gfs"
    assert data["default_cycle"] == "2026-09-02T12:00:00Z"
    assert [entry["cycle_time"] for entry in data["cycles"]] == [
        "2026-09-02T12:00:00Z",
        "2026-09-02T06:00:00Z",
    ]
    instants = [data["default_cycle"]]
    for entry in data["cycles"]:
        instants.extend([entry["cycle_time"], entry["valid_time_start"], entry["valid_time_end"]])
    assert all(_INSTANT_RE.match(instant) for instant in instants), instants


def test_valid_times_route_serves_the_requested_identity_in_the_pinned_spelling(monkeypatch: Any) -> None:
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    valid_times = response.json()["data"]["valid_times"]
    assert len(valid_times) == 57
    assert valid_times[0] == "2026-09-02T12:00:00Z"
    assert all(_INSTANT_RE.match(instant) for instant in valid_times), valid_times


def test_valid_times_route_without_arguments_keeps_serving_the_national_list(monkeypatch: Any) -> None:
    """The no-argument branch is a live route, not just an internal default.

    The frontend's `fetchLayerValidTimes` fallback still calls it with no
    arguments (`apps/frontend/src/stores/overviewData.ts`), so making
    `source`/`cycle` mandatory would break it. `scripts/node27_mvt_prewarm.py`
    no longer calls it -- since #2013 it always passes `source` and `cycle`.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers/discharge/valid-times")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    valid_times = response.json()["data"]["valid_times"]
    assert valid_times
    assert all(_INSTANT_RE.match(instant) for instant in valid_times), valid_times


def test_valid_times_cache_key_collapses_spellings_and_separates_identities(monkeypatch: Any) -> None:
    keys: list[str] = []
    options: list[dict[str, Any]] = []

    def _record(_request: Any, key: str, load: Any, **kwargs: Any) -> Any:
        keys.append(key)
        options.append(kwargs)
        return load()

    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", _record)
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for params in (
                {"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
                {"source": "gfs", "cycle": "2026-09-02T12:00:00.000Z"},
                # Same instant again, spelled with a non-UTC offset: only the
                # canonicalized value collapses this one onto the first two.
                {"source": "gfs", "cycle": "2026-09-02T20:00:00+08:00"},
                {"source": "gfs", "cycle": "2026-09-02T15:00:00Z"},
                {"source": "ifs", "cycle": "2026-09-02T12:00:00Z"},
            ):
                assert client.get("/api/v1/layers/discharge/valid-times", params=params).status_code == 200
    finally:
        app.dependency_overrides.clear()

    assert keys[0] == keys[1] == keys[2], keys
    assert len(set(keys)) == 3, keys
    # 客户端可控的 `cycle` 维度必须带准入谓词（空 valid_times 不入缓存）。
    assert all("cacheable" in option for option in options), options


def test_cycles_cache_key_separates_the_two_sources(monkeypatch: Any) -> None:
    """The only red-capable oracle for this route's key.

    `display_catalog_cached` returns `loader()` directly unless `display_readonly`
    is on, and this route's other test discards the key entirely -- so replacing
    it with a constant is green everywhere locally while node-27 serves the gfs
    intersection under `?source=ifs` for a whole cache window.
    """
    keys: list[str] = []
    options: list[dict[str, Any]] = []

    def _record(_request: Any, key: str, load: Any, **kwargs: Any) -> Any:
        keys.append(key)
        options.append(kwargs)
        return load()

    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", _record)
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            for source in ("gfs", "ifs"):
                response = client.get("/api/v1/layers/discharge/cycles", params={"source": source})
                assert response.status_code == 200, response.text
    finally:
        app.dependency_overrides.clear()

    assert keys == ["discharge-cycles:gfs", "discharge-cycles:ifs"]
    # `source` 是两值 `Literal`（有界维度），且 fail-closed 的 `cycles: []` 是正当答案：
    # 这条路由**不**传准入谓词，否则空交集期间每次请求都要重跑那趟发现查询。
    assert all("cacheable" not in option for option in options), options


@pytest.mark.parametrize(
    ("case", "path", "params"),
    [
        ("cycle-without-source", "/api/v1/layers/discharge/valid-times", {"cycle": "2026-09-02T12:00:00Z"}),
        ("source-without-cycle", "/api/v1/layers/discharge/valid-times", {"source": "gfs"}),
        (
            "run-id-with-identity",
            "/api/v1/layers/discharge/valid-times",
            {"source": "gfs", "cycle": "2026-09-02T12:00:00Z", "run_id": "run_1"},
        ),
        (
            "identity-on-another-layer",
            "/api/v1/layers/river-network/valid-times",
            {"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
        ),
        (
            "sub-second-cycle",
            "/api/v1/layers/discharge/valid-times",
            {"source": "gfs", "cycle": "2026-09-02T12:00:00.500Z"},
        ),
        (
            "unshaped-cycle",
            "/api/v1/layers/discharge/valid-times",
            {"source": "gfs", "cycle": "2026-09-02 12:00:00"},
        ),
        ("unknown-source", "/api/v1/layers/discharge/valid-times", {"source": "ERA5", "cycle": "2026-09-02T12:00:00Z"}),
        ("cased-source", "/api/v1/layers/discharge/valid-times", {"source": "GFS", "cycle": "2026-09-02T12:00:00Z"}),
        ("cycles-unknown-source", "/api/v1/layers/discharge/cycles", {"source": "ERA5"}),
        ("cycles-best-source", "/api/v1/layers/discharge/cycles", {"source": "best"}),
        ("cycles-cased-source", "/api/v1/layers/discharge/cycles", {"source": "GFS"}),
        ("cycles-missing-source", "/api/v1/layers/discharge/cycles", {}),
    ],
)
def test_national_discovery_routes_reject_half_formed_selectors_before_any_sql(
    case: str, path: str, params: dict[str, str]
) -> None:
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(path, params=params)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR", response.text


# ---------------------------------------------------------------------------
# Post-gate site-rule oracles (#2009 review round 4). Appended at the END of the
# file on purpose: every line-number citation in the invariant matrix and in the
# mutation driver anchors on the cases above, so inserting between them would
# invalidate the fixture rather than extend it.
# ---------------------------------------------------------------------------


def test_national_per_cycle_valid_times_fail_closed_when_a_network_activates_between_the_two_statements() -> None:
    """The per-cycle twin of `test_national_cycles_fail_closed_when_a_network_activates...`.

    Row 40's oracle only ever touched the `national_discharge_cycles` site; this
    one drives the SECOND set comparison, the one inside
    `national_discharge_valid_times`' per-cycle branch. Same race, same shape:
    equal cardinality, different membership. Statement 1 sees the active set
    `{rn-b, rn-c1, rn-c2}`; `rn-a` is activated with a display-ready run for the
    requested cycle, so statement 2 returns `{rn-a, rn-c1, rn-c2}` while `rn-b`
    never had that cycle at all. `3 == 3`, so a cardinality comparison would
    serve `rn-b`'s basins a timeline they cannot render.
    """
    session = _NationalDiscoverySession(
        _full_coverage_rows(_CYCLE, networks=("rn-a", "rn-c1", "rn-c2")),
        active_networks=["rn-b", "rn-c1", "rn-c2"],
    )

    result = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    # Non-vacuity: the two statements really do disagree AT EQUAL SIZE, so a
    # cardinality comparison would pass where the set comparison fails.
    assert len(session.active_networks) == 3
    assert len({row["river_network_version_id"] for row in session.rows}) == 3
    assert result.valid_times == []
    assert result.observed_count == 0
    assert result.truncated is False


def test_no_argument_national_valid_times_are_empty_with_no_coverage_rows() -> None:
    """Zero coverage rows on the no-argument branch: `[]`, not a crash.

    Every other no-argument case in this file feeds the branch at least one row,
    so the `if not latest_by_network` guard had no oracle: deleting it lets the
    empty `coverage` list reach `max(start for start, _ in coverage)`, which
    raises `ValueError` and turns a fail-closed empty timeline into an HTTP 500.
    """
    session = _NationalDiscoverySession([], active_networks=["rn-a", "rn-b"])

    discovery = national_discharge_valid_times(session)

    assert discovery.valid_times == []
    assert discovery.observed_count == 0


def test_discharge_routes_pass_ifs_through_to_the_coverage_bind(monkeypatch: Any) -> None:
    """`?source=ifs` must reach the SQL bind on BOTH routes, not a `gfs` literal.

    The fixture separates the two sources by CYCLE (`gfs` at `_CYCLE`, `ifs` at
    `_PREVIOUS_CYCLE`), so any call site that hardcodes `gfs` -- the route's own
    `source=` argument, the response echo, or the helper's forward into
    `_national_discharge_coverage_rows` -- answers for the wrong cycle.

    The helper-level NEGATIVE half at the end is the only oracle for the
    forward at `services/tiles/mvt.py`'s per-cycle `_national_discharge_coverage_rows`
    call: dropping `source=source` binds `None`, which both the SQL and this
    file's fake read as "no filter", so the `ifs` half stays green while `gfs`
    at `_PREVIOUS_CYCLE` starts answering with the `ifs` rows.
    """
    rows = _full_coverage_rows(_CYCLE)
    rows.extend(
        _coverage_row(
            network=network,
            cycle=_PREVIOUS_CYCLE,
            start=_PREVIOUS_CYCLE,
            end=_PREVIOUS_CYCLE + timedelta(hours=168),
            source="ifs",
            run_id=f"run-ifs-{network}",
        )
        for network in ("rn-a", "rn-b", "rn-c")
    )
    session = _NationalDiscoverySession(rows)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load, **_: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            cycles_response = client.get("/api/v1/layers/discharge/cycles", params={"source": "ifs"})
            ifs_times = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "ifs", "cycle": canonical_mvt_time(_PREVIOUS_CYCLE)},
            )
            gfs_times = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": canonical_mvt_time(_PREVIOUS_CYCLE)},
            )
    finally:
        app.dependency_overrides.clear()

    assert cycles_response.status_code == 200, cycles_response.text
    body = cycles_response.json()["data"]
    assert body["source"] == "ifs"
    assert [entry["cycle_time"] for entry in body["cycles"]] == [canonical_mvt_time(_PREVIOUS_CYCLE)]
    assert body["default_cycle"] == canonical_mvt_time(_PREVIOUS_CYCLE)

    assert ifs_times.status_code == 200, ifs_times.text
    assert len(ifs_times.json()["data"]["valid_times"]) == 57
    assert gfs_times.status_code == 200, gfs_times.text
    assert gfs_times.json()["data"]["valid_times"] == []

    helper_ifs = national_discharge_valid_times(session, source="ifs", cycle=_PREVIOUS_CYCLE)
    helper_gfs = national_discharge_valid_times(session, source="gfs", cycle=_PREVIOUS_CYCLE)
    assert helper_ifs.valid_times
    assert helper_gfs.valid_times == []
    assert helper_gfs.observed_count == 0


def test_national_per_cycle_valid_times_are_not_truncated_when_observed_equals_the_limit() -> None:
    """`truncated` is `observed > limit`, strictly: a full-but-not-over list is complete.

    Today's cases sit at 57 < 100 or 57 > 5, so both spellings agree on them and
    `>=` would ship a list that IS the whole window while telling the frontend
    there is more behind it.
    """
    session = _NationalDiscoverySession(
        [
            _coverage_row(network=network, cycle=_CYCLE, start=_CYCLE, end=_CYCLE + timedelta(hours=12))
            for network in ("rn-a", "rn-b", "rn-c")
        ]
    )

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE, limit=5)

    assert len(discovery.valid_times) == 5
    assert discovery.observed_count == 5
    assert discovery.truncated is False


def test_national_per_cycle_valid_times_clamp_an_off_grid_window_inward() -> None:
    """Both clamp ends round INWARD: an advertised instant must be inside the coverage.

    `run_display_coverage` is an hourly grid, so a window can start and end off
    the 3-hour stride. `C+4h … C+97h` has its first stride instant at `C+6h` and
    its last at `C+96h`; rounding the start down would advertise `C+3h` (before
    any basin has data) and rounding the end up would advertise `C+99h` (after
    the earliest coverage end).
    """
    session = _NationalDiscoverySession(
        [
            _coverage_row(
                network=network,
                cycle=_CYCLE,
                start=_CYCLE + timedelta(hours=4),
                end=_CYCLE + timedelta(hours=97),
            )
            for network in ("rn-a", "rn-b", "rn-c")
        ]
    )

    discovery = national_discharge_valid_times(session, source="gfs", cycle=_CYCLE)

    assert discovery.valid_times[0] == canonical_mvt_time(_CYCLE + timedelta(hours=6))
    assert discovery.valid_times[-1] == canonical_mvt_time(_CYCLE + timedelta(hours=96))
    assert discovery.observed_count == 31


def test_national_cycles_pass_their_limit_to_the_per_cycle_clamp() -> None:
    """`national_discharge_cycles(limit=)` must reach the per-cycle stride computation.

    No route or catalog call site passes `limit` to this function, so the
    argument's only forwarding site was unobserved: hardcoding the module
    constant there leaves every existing case green (a 168 h rectangle yields 57
    entries, under the constant 100). With `limit=5` the retained list stops at
    the fifth stride instant and the listed window's END is what shows it.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    result = national_discharge_cycles(session, source="gfs", limit=5)

    assert [entry["cycle_time"] for entry in result["cycles"]] == [canonical_mvt_time(_CYCLE)]
    assert result["cycles"][0]["valid_time_start"] == canonical_mvt_time(_CYCLE)
    assert result["cycles"][0]["valid_time_end"] == canonical_mvt_time(_CYCLE + timedelta(hours=12))


def test_national_coverage_statements_pin_their_shape() -> None:
    """TRIPWIRES, not oracles, for the coverage query's SQL shape.

    `_NationalDiscoverySession` never parses SQL: it matches on a table name and
    filters its canned rows by the BOUND values, so every predicate and window
    clause below is invisible to it for any row data whatsoever. The behavioural
    oracle for seven of these eight literals is the node-27 integration file
    (`tests/test_mvt_national_identity_probe_integration.py`, matrix rows 50-56);
    what this case buys is a loud local signal the moment one of them is edited
    or deleted, so the change cannot reach review looking untouched.

    The eighth, `AND rdc.segment_count > 0`, is matrix row 3, and this assertion
    is its ONLY local signal -- its behavioural oracle is likewise on node-27
    (`test_national_cycles_keep_a_cycle_whose_zero_segment_rival_run_sorts_first`).

    `SELECT DISTINCT` is asserted as SHAPE only: as enumeration site 50 it is
    excluded as result-equivalent, so its presence here is a tripwire and never
    a behavioural claim.

    The `:since` predicate already has its own pin in
    `test_national_cycles_list_only_cycles_inside_the_lookback_window` and is
    deliberately not repeated here.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))

    national_discharge_cycles(session, source="gfs")

    active_sql = next(
        sql
        for sql, _ in session.executions
        if "core.model_instance mi" in sql and "hydro.hydro_run" not in sql
    )
    coverage_sql = next(sql for sql, _ in session.executions if "hydro.run_display_coverage" in sql)

    # The denominator: only ACTIVE instances, and only those naming a network.
    # `AND mi.river_network_version_id IS NOT NULL` also occurs in the coverage
    # statement, so it is asserted against the active statement alone.
    assert "SELECT DISTINCT mi.river_network_version_id" in active_sql
    assert "WHERE mi.active_flag" in active_sql
    assert "AND mi.river_network_version_id IS NOT NULL" in active_sql

    # The ranked read: one winner per (network, cycle), newest run first, both
    # identity conjuncts present, and `rn = 1` selecting that winner.
    assert _SOURCE_CONJUNCT in coverage_sql
    assert _CYCLE_CONJUNCT in coverage_sql
    assert "PARTITION BY mi.river_network_version_id, h.cycle_time" in coverage_sql
    assert "ORDER BY h.run_id DESC" in coverage_sql
    assert "WHERE rn = 1" in coverage_sql

    # The join-side emptiness filter: a coverage row with no segments is not a
    # candidate at all, so it cannot win its (network, cycle) partition.
    assert "AND rdc.segment_count > 0" in coverage_sql


# --- #2078：display 角色下的目录缓存准入与 layers 后切片 -------------------------


def _display_role_catalog_app(monkeypatch: Any, session: Any, tmp_path: Path) -> Any:
    """真正会缓存的 `/api/v1/layers` 应用：display_readonly 角色，不替换缓存函数。

    别的用例用 `main.create_app()`（DEV_MONOLITH）+ 替身缓存，那条路径上
    `display_catalog_cached` 直通 loader，缓存 key 与准入谓词根本不被执行。
    角色 env 显式交给 `create_app`（照 `tests/test_precip_overlay.py` 的建法），
    这样 display 边界检查只看见这三个变量。`create_app` 起的预热线程由 conftest 的
    autouse fixture 停掉，缓存也在每个用例之间清空。
    """
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_latest"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a"))
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display, "_mvt_live_postgis_enabled", lambda _s: False)
    app = main.create_app(
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            "OBJECT_STORE_ROOT": str(object_store_root),
        }
    )
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: session
    return app


def _three_layers() -> list[Any]:
    return [
        hydro_display.Layer(
            layer_id=f"layer-{index}",
            layer_name=f"Layer {index}",
            layer_type="vector",
            variables=[f"var-{index}"],
            metadata={"index": index},
        )
        for index in range(3)
    ]


def test_layers_pagination_is_applied_after_the_cache(monkeypatch: Any, tmp_path: Path) -> None:
    """三次不同分页只建一次目录，key 不含 limit/offset，越界 offset 是不落 DB 的空页。"""
    layers = _three_layers()
    builds: list[dict[str, Any]] = []

    def _catalog(_session: Any, **kwargs: Any) -> list[Any]:
        builds.append(kwargs)
        return layers

    monkeypatch.setattr(hydro_display, "_default_layer_catalog", _catalog)
    app = _display_role_catalog_app(monkeypatch, _NationalDiscoverySession([]), tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            first_page = client.get("/api/v1/layers", params={"offset": 0, "limit": 2})
            second_page = client.get("/api/v1/layers", params={"offset": 1, "limit": 1})
            beyond = client.get("/api/v1/layers", params={"offset": 5})
    finally:
        app.dependency_overrides.clear()

    # 逐字节 oracle：切片必须等于「完整目录的 model_dump 列表再切片」。
    dumped = [layer.model_dump() for layer in layers]
    assert first_page.status_code == 200, first_page.text
    assert first_page.json()["data"] == dumped[0:2]
    assert second_page.status_code == 200, second_page.text
    assert second_page.json()["data"] == dumped[1:2]
    assert beyond.status_code == 200, beyond.text
    assert beyond.json()["data"] == dumped[5:105] == []

    assert len(builds) == 1, builds
    assert list(display_cache._store) == ["layers:None"]
    assert display_cache._store["layers:None"][1] == dumped


def test_a_literal_run_id_none_does_not_fold_into_the_national_layer_cache_entry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """`?run_id=None` 是通过标识符校验的普通字面量，不是「没给 run_id」。

    改前红：key 用裸 `f"layers:{run_id}"` 插值，字面量 `None` 与国家级请求同 key ——
    公网可以拿到国家级目录（本用例里该 run 根本不存在，应当 404），并顺手把国家级
    条目的热 path 改写成 `/api/v1/layers?run_id=None`，让预热线程每 tick 回放它。
    """
    monkeypatch.setattr(hydro_display, "_default_layer_catalog", lambda _session, **_kwargs: _three_layers())
    app = _display_role_catalog_app(monkeypatch, _NationalDiscoverySession([]), tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            national = client.get("/api/v1/layers")
            assert national.status_code == 200, national.text
            assert display_cache._hot_paths["layers:None"][0] == "/api/v1/layers"

            literal = client.get("/api/v1/layers", params={"run_id": "None"})
    finally:
        app.dependency_overrides.clear()

    # `_require_display_ready` -> `_run_row` 找不到该 run：未知 run 就是 404，
    # 而不是别人的目录。
    assert literal.status_code == 404, literal.text
    assert literal.json()["error"]["code"] == "RUN_NOT_FOUND"
    # loader 抛在写缓存之前，所以这个 key 什么也没留下；国家级条目的热 path 不被劫持。
    assert "layers:'None'" not in display_cache._store
    assert display_cache._hot_paths["layers:None"][0] == "/api/v1/layers"


def test_a_literal_run_id_none_does_not_fold_into_the_national_valid_times_entry(
    monkeypatch: Any, tmp_path: Path
) -> None:
    """同一族缺陷的 valid-times 面：`?run_id=None` 不是「没给 run_id」。

    改前红：key 用裸 `f"valid-times:{layer_id}:{requested_run_id}:…"`，字面量 `None`
    折叠进国家级条目 —— 公网拿到的是国家级 valid_times（该 run 根本不存在，应当 404），
    国家级条目的热 path 还被改写成一个必然 404 的 URL：预热回放只会抛错，条目再也
    刷不新（refresh starvation）。
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _display_role_catalog_app(monkeypatch, session, tmp_path)
    national_key = "valid-times:discharge:None:None:None"
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            national = client.get("/api/v1/layers/discharge/valid-times")
            assert national.status_code == 200, national.text
            assert national.json()["data"]["valid_times"]
            assert display_cache._hot_paths[national_key][0] == "/api/v1/layers/discharge/valid-times"

            literal = client.get("/api/v1/layers/discharge/valid-times", params={"run_id": "None"})
    finally:
        app.dependency_overrides.clear()

    # `_require_display_ready` -> `_run_row` 找不到该 run：未知 run 就是 404。
    assert literal.status_code == 404, literal.text
    assert literal.json()["error"]["code"] == "RUN_NOT_FOUND"
    assert "valid-times:discharge:'None':None:None" not in display_cache._store
    assert display_cache._hot_paths[national_key][0] == "/api/v1/layers/discharge/valid-times"


def test_empty_valid_times_are_not_cached_while_a_covered_cycle_is(monkeypatch: Any, tmp_path: Path) -> None:
    """交集外的 cycle 是客户端可控的无界 key 维度：空列表照常 200，但不留缓存条目。"""
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    app = _display_role_catalog_app(monkeypatch, session, tmp_path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            uncovered = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": "2026-09-01T00:00:00Z"},
            )
            covered = client.get(
                "/api/v1/layers/discharge/valid-times",
                params={"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
            )
    finally:
        app.dependency_overrides.clear()

    assert uncovered.status_code == 200, uncovered.text
    assert uncovered.json()["data"]["valid_times"] == []
    uncovered_key = "valid-times:discharge:None:'gfs':'2026-09-01T00:00:00Z'"
    assert uncovered_key not in display_cache._store
    assert uncovered_key not in display_cache._hot_paths

    assert covered.status_code == 200, covered.text
    covered_valid_times = covered.json()["data"]["valid_times"]
    assert covered_valid_times
    covered_key = "valid-times:discharge:None:'gfs':'2026-09-02T12:00:00Z'"
    assert display_cache._store[covered_key][1]["valid_times"] == covered_valid_times
    assert covered_key in display_cache._hot_paths


# ---------------------------------------------------------------------------
# #2033: a user-supplied tile instant never yields a 5xx. Appended at the END
# for the same reason the block above says: the fixture's anchors are ordinal.
# ---------------------------------------------------------------------------

# Well-formed RFC3339 whose shift to UTC leaves `datetime.max` / `datetime.min`.
# CPython answers `OverflowError` -- NOT `ValueError` -- so nothing on the tile
# path caught it and both legacy routes answered 500 (measured on
# `https://test.nwm.ac.cn`, 2026-09-08).
_OUT_OF_UTC_RANGE_INSTANTS = ("9999-12-31T23:59:59-08:00", "0001-01-01T00:00:00+08:00")

# The SAME extremes spelled NAIVE. `canonical_mvt_time` reads a naive instant as
# UTC, so no shift happens and neither one can overflow: they must stay
# ACCEPTED. This pair is what fails if the guard is written as
# `value.astimezone(UTC)` unconditionally.
_NAIVE_EXTREME_INSTANTS = ("9999-12-31T23:59:59", "0001-01-01T00:00:00")

_UNREPRESENTABLE_INSTANT_MESSAGE = "Tile time instants must be representable in UTC."

_LEGACY_RUN_ID = "run_a"


def _legacy_run_url(valid_time: str = "2026-09-03T00:00:00Z") -> str:
    """The legacy single-run alias `/api/v1/tiles/hydro/{run_id}/...`."""
    return (
        f"/api/v1/tiles/hydro/{_LEGACY_RUN_ID}/q_down/{valid_time}"
        f"/{_NATIONAL_TILE_Z}/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"
    )


class _CountingNationalRouteSession(_NationalRouteSession):
    """`_NationalRouteSession` plus the counter that makes `sql=0` a MEASURED claim.

    `_ExplodingSession` proves "no statement ran" only by turning one into a
    500; acceptance criterion 3 asks for the count itself, because the measured
    pre-fix baseline was `sql=1` on this route (the `source_version=` kwarg
    calls `national_discharge_source_version` before `valid_time=` is ever
    formatted).
    """

    def __init__(self, digest_rows: list[dict[str, Any]] | None = None) -> None:
        super().__init__(digest_rows)
        self.execute_count = 0

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        self.execute_count += 1
        return super().execute(statement, params)


class _LegacyRunRouteSession:
    """Answers -- and counts -- the three statements `hydro_mvt_tile` issues.

    `_run_row` (display-ready lookup), `_require_hydro_mvt_source_identity`
    (existence probe) and the tile SQL, in that order. The pre-fix baseline on
    this route was two statements before the 500.
    """

    _RUN_ROW = {
        "run_id": _LEGACY_RUN_ID,
        "status": "published",
        "model_id": "model_a",
        "basin_version_id": "bv_a",
        "source_id": "gfs",
        "cycle_time": "2026-09-02T12:00:00Z",
        "updated_at": "2026-09-02T13:00:00Z",
        "river_network_version_id": "rnv_a",
        "timeseries_store": "legacy",
    }

    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.execute_count = 0
        self.tile_params: list[dict[str, Any]] = []
        self.executions: list[tuple[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        self.execute_count += 1
        sql = str(statement)
        self.executions.append((sql, params))
        if "ST_AsMVT" in sql:
            self.tile_params.append(dict(params or {}))
            return _TileResult([dict(_NationalRouteSession._TILE_ROW)])
        if "FROM hydro.hydro_run h" in sql:
            return _TileResult([dict(self._RUN_ROW)])
        return _TileResult([{"exists": 1}])

    def get_bind(self) -> Any:
        return self.bind


@pytest.mark.parametrize("store", ("legacy", "narrow"))
@pytest.mark.parametrize("found", (True, False), ids=("found", "not-found"))
def test_hydro_mvt_probe_routes_one_store_and_preserves_not_found(store: str, found: bool) -> None:
    session = _Session([{"exists": 1}] if found else [])
    arguments = {
        "run_id": _LEGACY_RUN_ID,
        "variable": "q_down",
        "valid_time": datetime(2026, 9, 3, tzinfo=UTC),
        "basin_version_id": "bv_a",
        "river_network_version_id": "rnv_a",
    }
    if found:
        hydro_display._require_hydro_mvt_source_identity(session, **arguments, timeseries_store=store)
    else:
        with pytest.raises(ApiError) as raised:
            hydro_display._require_hydro_mvt_source_identity(session, **arguments, timeseries_store=store)
        assert raised.value.status_code == 404
        assert raised.value.code == "MVT_SOURCE_IDENTITY_NOT_FOUND"
        assert raised.value.details == {
            **arguments,
            "layer_id": "discharge",
            "valid_time": "2026-09-03T00:00:00Z",
        }

    assert len(session.executions) == 1
    sql, params = session.executions[0]
    raw = entry_by_key("hydro_display:mvt_source_identity_probe").source(store)
    assert sql == render_river_ts_sql(raw, store, entry="hydro_display:mvt_source_identity_probe").sql
    assert re.findall(r"\bFROM\s+hydro\.(river_timeseries\w*)\b", sql) == [
        "river_timeseries_legacy" if store == "legacy" else "river_timeseries"
    ]
    assert "UNION" not in sql.upper()
    assert len(re.findall(r"\bLIMIT\s+1\b", sql, re.IGNORECASE)) == 1
    assert params == arguments
    assert set(re.findall(r"(?<!:):([a-z_]+)", sql)) == set(arguments)
    assert session.results[0].first_count == 1
    for column in ("run_key", "basin_version_key", "river_network_version_key", "variable_e"):
        assert re.search(rf"\b{column}\s*=\s*\(", sql)
    assert "unnest(enum_range(NULL::hydro.river_variable))" in sql
    assert "AND valid_time = :valid_time" in sql
    for column in ("run_id", "river_network_version_id", "variable"):
        assert bool(re.search(rf"\bAND {column} = :{column}\b", sql)) is (store == "legacy")
    assert sql.count("transitional compressed-chunk pushdown aid") == (3 if store == "legacy" else 0)


@pytest.mark.parametrize(
    "metadata",
    ({"timeseries_store": None}, {"timeseries_store": "unknown"}, {"timeseries_store": ["legacy"]}),
    ids=("missing-or-null", "unknown", "non-string"),
)
def test_hydro_mvt_probe_rejects_invalid_store_before_sql(metadata: dict[str, Any]) -> None:
    session = _Session([])
    with pytest.raises(ApiError) as raised:
        hydro_display._require_hydro_mvt_source_identity(
            session,
            run_id=_LEGACY_RUN_ID,
            variable="q_down",
            valid_time=datetime(2026, 9, 3, tzinfo=UTC),
            basin_version_id="bv_a",
            river_network_version_id="rnv_a",
            timeseries_store=metadata.get("timeseries_store"),
        )
    assert raised.value.status_code == 500
    assert raised.value.code == "TIMESERIES_STORE_INVALID"
    assert raised.value.details == {"run_id": _LEGACY_RUN_ID}
    assert session.executions == []


@pytest.mark.parametrize(
    "metadata, status, code",
    (
        pytest.param({"status": "running"}, 409, "DISPLAY_PRODUCT_NOT_READY", id="not-ready-missing"),
        pytest.param(
            {"status": "running", "timeseries_store": "unknown"},
            409, "DISPLAY_PRODUCT_NOT_READY", id="not-ready-invalid",
        ),
        pytest.param(
            {"basin_version_id": None}, 404, "MVT_SOURCE_IDENTITY_NOT_FOUND", id="no-basin-missing",
        ),
        pytest.param(
            {"river_network_version_id": None, "timeseries_store": "unknown"},
            404, "MVT_SOURCE_IDENTITY_NOT_FOUND", id="no-network-invalid",
        ),
        pytest.param({}, 500, "TIMESERIES_STORE_INVALID", id="ready-missing"),
        pytest.param({"timeseries_store": None}, 500, "TIMESERIES_STORE_INVALID", id="ready-null"),
        pytest.param({"timeseries_store": "unknown"}, 500, "TIMESERIES_STORE_INVALID", id="ready-invalid"),
        pytest.param({"timeseries_store": "legacy"}, 200, None, id="ready-legacy"),
        pytest.param({"timeseries_store": "narrow"}, 200, None, id="ready-narrow"),
    ),
)
def test_hydro_mvt_route_preserves_store_error_precedence_and_sql_order(
    metadata: dict[str, Any], status: int, code: str | None, monkeypatch: Any, tmp_path: Path,
) -> None:
    session = _LegacyRunRouteSession()
    session._RUN_ROW = {key: value for key, value in session._RUN_ROW.items() if key != "timeseries_store"}
    session._RUN_ROW.update(metadata)
    response, captured = _request_national_identity_tile(_legacy_run_url(), session, monkeypatch, tmp_path)

    assert response.status_code == status, response.text
    assert session.execute_count == (3 if status == 200 else 1)
    assert len(session.executions) == session.execute_count
    metadata_sql, metadata_params = session.executions[0]
    assert "FROM hydro.hydro_run h" in metadata_sql
    assert metadata_params == {"run_id": _LEGACY_RUN_ID}
    if status != 200:
        assert response.json()["error"]["code"] == code
        if status == 500:
            assert response.json()["error"]["details"] == {"run_id": _LEGACY_RUN_ID}
        assert captured == []
        assert session.tile_params == []
        return

    store = metadata["timeseries_store"]
    probe_sql, probe_params = session.executions[1]
    raw = entry_by_key("hydro_display:mvt_source_identity_probe").source(store)
    assert probe_sql == render_river_ts_sql(raw, store, entry="hydro_display:mvt_source_identity_probe").sql
    assert re.findall(r"\bFROM\s+hydro\.(river_timeseries\w*)\b", probe_sql) == [
        "river_timeseries_legacy" if store == "legacy" else "river_timeseries"
    ]
    assert probe_params == {
        "run_id": _LEGACY_RUN_ID,
        "variable": "q_down",
        "valid_time": datetime(2026, 9, 3, tzinfo=UTC),
        "basin_version_id": "bv_a",
        "river_network_version_id": "rnv_a",
    }
    assert "ST_AsMVT" in session.executions[2][0]
    assert response.headers["content-type"] == "application/x-protobuf"
    assert response.headers["x-tile-layer-id"] == "discharge"


def _legacy_route_case(route: str) -> tuple[Any, Any]:
    """`(session, url_builder)` for one of the two legacy tile routes."""
    if route == "national":
        return _CountingNationalRouteSession(), _legacy_national_url
    return _LegacyRunRouteSession(), _legacy_run_url


@pytest.mark.parametrize("form", ["datetime", "str"])
@pytest.mark.parametrize("instant", _OUT_OF_UTC_RANGE_INSTANTS)
def test_canonical_mvt_time_raises_a_typed_error_outside_the_utc_range(form: str, instant: str) -> None:
    """Task 1.2 / spec "Helper raises a typed error rather than OverflowError".

    BOTH branches: the `isinstance(value, datetime)` one and the one that runs
    after `_parse_iso_datetime`. A `str` here is not a formality -- the cache-row
    comparison in `mvt.py::_read_cache` feeds this helper raw column values.
    """
    value: Any = datetime.fromisoformat(instant) if form == "datetime" else instant

    with pytest.raises(mvt_module.MvtTimeOutOfRangeError) as excinfo:
        canonical_mvt_time(value)

    # The TYPE, not just "some ValueError": `ValueError` is the base class so
    # existing callers keep working, and the subclass is what the route layer
    # translates precisely.
    assert type(excinfo.value) is mvt_module.MvtTimeOutOfRangeError
    assert isinstance(excinfo.value, ValueError)
    # Chained, never swallowed: the CPython original stays diagnosable.
    assert isinstance(excinfo.value.__cause__, OverflowError)


def test_canonical_mvt_time_is_unchanged_for_every_in_range_and_unparseable_value() -> None:
    """The no-shift half of the contract: nothing about canonicalization moves."""
    for spelling in (
        "2026-09-03T00:00:00Z",
        "2026-09-03T00:00:00+00:00",
        "2026-09-03T00:00:00.000Z",
        "2026-09-03T08:00:00+08:00",
    ):
        assert canonical_mvt_time(spelling) == "2026-09-03T00:00:00Z", spelling
        assert canonical_mvt_time(datetime.fromisoformat(spelling.replace("Z", "+00:00"))) == "2026-09-03T00:00:00Z"

    # Sub-second still round-trips rather than truncating (that is what makes
    # the legacy routes' acceptance of it observable at all).
    assert canonical_mvt_time("2026-09-03T00:00:00.500Z") == "2026-09-03T00:00:00.500000Z"
    # Space-separated DB spelling, `None`, and text that is not an instant.
    assert canonical_mvt_time("2026-09-03 00:00:00+00:00") == "2026-09-03T00:00:00Z"
    assert canonical_mvt_time(None) is None
    assert canonical_mvt_time("not-an-instant") == "not-an-instant"
    # The naive extremes: read as UTC, no shift, so the new branch must NOT fire.
    for naive in _NAIVE_EXTREME_INSTANTS:
        assert canonical_mvt_time(naive) == f"{naive}Z", naive
        assert canonical_mvt_time(datetime.fromisoformat(naive)) == f"{naive}Z", naive


@pytest.mark.parametrize("instant", _OUT_OF_UTC_RANGE_INSTANTS)
@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_reject_an_unrepresentable_instant_before_any_sql(
    route: str, instant: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Acceptance criteria 2 and 3, on the two routes that answered 500.

    `execute_count == 0` is the discriminating assertion: the pre-fix national
    route paid a `national_discharge_source_version` round trip and the
    single-run route paid `_require_display_ready` +
    `_require_hydro_mvt_source_identity` BEFORE raising.
    """
    session, url_for = _legacy_route_case(route)
    response, _ = _request_national_identity_tile(
        url_for(quote(instant, safe="")), session, monkeypatch, tmp_path
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", response.text
    # The MESSAGE too: the three tile routes share one validator precisely so
    # they cannot drift on the error body.
    assert error["message"] == _UNREPRESENTABLE_INSTANT_MESSAGE, response.text
    # The whole `details` body, not just code + message: the field name is what
    # points the client at the offending path segment, and the two legacy call
    # sites pass it as an independent literal each. `_require_representable_instant`
    # echoes `value.isoformat()` of the pydantic-parsed aware datetime, which
    # round-trips the requested offset spelling verbatim.
    assert error["details"] == {
        "valid_time": datetime.fromisoformat(instant).isoformat(),
        "expected_format": "YYYY-MM-DDTHH:MM:SSZ",
    }, response.text
    assert session.execute_count == 0


@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_keep_their_unparseable_and_in_range_verdicts(
    route: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Regression either side of the new branch: FastAPI's 422 and the SQL path."""
    session, url_for = _legacy_route_case(route)
    response, _ = _request_national_identity_tile(url_for("not-an-instant"), session, monkeypatch, tmp_path)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "VALIDATION_ERROR", response.text
    # Unchanged: pydantic rejects it before the route body, so still no SQL.
    assert session.execute_count == 0

    session, url_for = _legacy_route_case(route)
    response, captured = _request_national_identity_tile(
        url_for("2026-09-03T00:00:00Z"), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert session.execute_count > 0
    assert captured[0].valid_time == "2026-09-03T00:00:00Z"


@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_keep_accepting_an_in_range_sub_second_instant(
    route: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Design D3: the legacy routes get the RANGE guard, never the sub-second one.

    Reusing `_require_seconds_precision_instant` here would turn this 200 into a
    422 -- a behavior break on the very routes this change promises to leave
    alone. This is the case that fails if someone "shares one validator" too far.
    """
    session, url_for = _legacy_route_case(route)
    response, captured = _request_national_identity_tile(
        url_for(quote("2026-09-03T00:00:00.500Z", safe="")), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert captured[0].valid_time == "2026-09-03T00:00:00.500000Z"


@pytest.mark.parametrize("instant", _NAIVE_EXTREME_INSTANTS)
@pytest.mark.parametrize("route", ["national", "run"])
def test_legacy_tile_routes_still_accept_a_naive_extreme_instant(
    route: str, instant: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Task 3.11a. The naive branch of the guard is load-bearing.

    `valid_time: datetime` is lax on both legacy aliases, so a naive instant is a
    real input class. `value.replace(tzinfo=UTC)` cannot overflow; a guard
    written as `value.astimezone(UTC)` would reinterpret it in SERVER-LOCAL time
    and newly 422 these two -- platform-dependent, and invisible to every
    tz-aware case above.

    `== 200`, not `!= 422`: the spec requirement is "a user-supplied tile instant
    never produces a 5xx", and `!= 422` is satisfied by the 500 it forbids. The
    cache read that fills `captured` happens BEFORE the producer runs, so
    `execute_count > 0` and the `captured[0]` assertion are both already
    satisfied by then -- a producer-side exception left all three green.
    """
    session, url_for = _legacy_route_case(route)
    response, captured = _request_national_identity_tile(
        url_for(quote(instant, safe="")), session, monkeypatch, tmp_path
    )

    assert response.status_code == 200, response.text
    assert session.execute_count > 0
    assert captured[0].valid_time == f"{instant}Z"


# `cache_key` digests computed by `origin/master`'s `services/tiles/mvt.py`
# (which this branch leaves byte-identical apart from the new failure branch).
# Recorded as literals rather than recomputed from the module under test, so the
# assertion has an oracle outside the code it guards.
_MASTER_NATIONAL_TILE_BASIS: dict[str, Any] = {
    "layer_id": "discharge",
    "source_id": "hydro-national",
    "source_version": "hydro-national-latest-per-basin-stream-type-v3:national-hydro-digest",
    "z": 4,
    "x": 13,
    "y": 6,
    "variant_id": "variable:q_down",
}
_MASTER_NATIONAL_CACHE_KEY = "30470851130440a8aa8144e4a5ecb1dbe402a5020ed00467f41a9f2c4cd119ab"

_MASTER_SIBLING_TILE_BASES: dict[str, dict[str, Any]] = {
    "river-network-national": {
        "layer_id": "river-network",
        "source_id": "river-network-national",
        "source_version": "river-network-national-digest",
        "valid_time": None,
        "z": 4,
        "x": 13,
        "y": 6,
        "variant_id": "national",
    },
    "river-network": {
        "layer_id": "river-network",
        "source_id": "bv_a",
        "source_version": "river-network-basin-digest",
        "valid_time": None,
        "z": 4,
        "x": 13,
        "y": 6,
    },
    "met-stations": {
        "layer_id": "met-stations",
        "source_id": "bv_a",
        "source_version": "met-stations-digest",
        "valid_time": None,
        "z": 4,
        "x": 13,
        "y": 6,
    },
}
_MASTER_SIBLING_CACHE_KEYS = {
    "river-network-national": "6db92a86bbbfd96f2cbedefd292dc63fb073a8a65e6717f534dfff34033d962c",
    "river-network": "d78922eaed1befeb60f8976ce374d5c62dda1bddd7fb8f93a09ad7ab672a2685",
    "met-stations": "2f0b7cc7e02c6e99114b9700254c92fc7068ecbd3cc8ac93d33edf0055561d99",
}


@pytest.mark.parametrize(
    "spelling",
    ["2026-09-03T00:00:00Z", "2026-09-03T00:00:00+00:00", "2026-09-03T00:00:00.000Z", "2026-09-03T08:00:00+08:00"],
)
def test_in_range_instant_spellings_keep_the_cache_key_master_computes(spelling: str) -> None:
    """Published-artifact identity: the four in-range spellings still collapse onto
    ONE digest, and that digest is the one `origin/master` produced."""
    assert cache_key(TileInput(valid_time=spelling, **_MASTER_NATIONAL_TILE_BASIS)) == _MASTER_NATIONAL_CACHE_KEY


@pytest.mark.parametrize("layer", sorted(_MASTER_SIBLING_TILE_BASES))
def test_valid_time_less_sibling_layers_keep_the_cache_key_master_computes(layer: str) -> None:
    """Task 3.8, digest half: the three `valid_time=None` layers are untouched.

    `canonical_mvt_time(None)` returns before either guarded branch, so the new
    failure path cannot be reached from them at all -- pinned rather than argued.
    """
    assert cache_key(TileInput(**_MASTER_SIBLING_TILE_BASES[layer])) == _MASTER_SIBLING_CACHE_KEYS[layer]


@pytest.mark.parametrize(
    ("layer", "url"),
    [
        ("river-network-national", f"/api/v1/tiles/river-network-national/{_NATIONAL_TILE_Z}"
         f"/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"),
        ("river-network", f"/api/v1/tiles/river-network/bv_a/{_NATIONAL_TILE_Z}"
         f"/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"),
        ("met-stations", f"/api/v1/tiles/met-stations/bv_a/{_NATIONAL_TILE_Z}"
         f"/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"),
    ],
)
def test_valid_time_less_sibling_tile_routes_still_answer_the_same_bytes(
    layer: str, url: str, monkeypatch: Any, tmp_path: Any
) -> None:
    """Task 3.8, response half: the three sibling routes take no instant at all."""
    session = _NationalRouteSession()
    response, captured = _request_national_identity_tile(url, session, monkeypatch, tmp_path)

    assert response.status_code == 200, response.text
    assert response.content == b"pbf-bytes"
    assert captured[0].valid_time is None
    assert captured[0].layer_id == ("met-stations" if layer == "met-stations" else "river-network")


@pytest.mark.parametrize("instant", _OUT_OF_UTC_RANGE_INSTANTS)
def test_valid_times_route_keeps_its_unrepresentable_cycle_verdict(instant: str) -> None:
    """Task 3.9: the refactored validator is reached from a SIXTH route.

    `source` AND `cycle` are both given on purpose: a `cycle`-only request is
    rejected by `_validated_national_valid_time_selector`'s "source and cycle
    must be given together." branch and never reaches the validator, so it has
    zero oracle power here. The MESSAGE is asserted for the same reason -- the
    code alone cannot tell those two 422 branches apart.
    """
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: _ExplodingSession()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/api/v1/layers/discharge/valid-times", params={"source": "gfs", "cycle": instant}
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR", response.text
    assert error["message"] == _UNREPRESENTABLE_INSTANT_MESSAGE, response.text


def test_seconds_precision_validator_still_returns_a_utc_normalized_instant() -> None:
    """Task 3.11 / design D4c: the RETURN VALUE is the contract, not a detail.

    `precip.py::_require_whole_hour_instant` reads `.minute`/`.second` off this
    return, and `_RFC3339_INSTANT_RE` accepts half-hour offsets. An extraction
    that returned the caller's ORIGINAL object would let
    `2026-09-02T20:00:00+05:30` (= 14:30 UTC) pass the whole-hour gate and be
    floored to hour 14 by `cycle_token` -- the exact cache poisoning that gate
    exists to prevent, and no error-path test would notice.
    """
    shifted = hydro_display._require_seconds_precision_instant(
        datetime.fromisoformat("2026-09-02T20:00:00+05:30"), "cycle"
    )

    assert shifted.utcoffset() == timedelta(0)
    assert (shifted.hour, shifted.minute) == (14, 30)
    assert shifted == datetime(2026, 9, 2, 14, 30, tzinfo=UTC)

    # The naive branch: read as UTC, never as server-local time.
    naive = hydro_display._require_seconds_precision_instant(datetime(2026, 9, 2, 20, 0), "cycle")

    assert naive == datetime(2026, 9, 2, 20, 0, tzinfo=UTC)
    assert naive.utcoffset() == timedelta(0)


def test_per_basin_store_union_preserves_the_frozen_public_sql_contract() -> None:
    frozen = (Path(__file__).parent / "fixtures/hydro_mvt_pre_store_f33441a2.sql").read_text()
    current = postgis_tile_sql("hydro")
    opener = "source_rows AS NOT MATERIALIZED ("
    closer = "\n        ),\n        source_identity_stats AS ("
    before, rest = frozen.split(opener, 1)
    original_source, after = rest.split(closer, 1)
    actual_before, rest = current.split(opener, 1)
    routed_source, actual_after = rest.split(closer, 1)
    assert actual_before == before
    assert actual_after == after
    branches = routed_source.split("\nUNION ALL\n")
    assert len(branches) == 2
    for store, branch in zip(("legacy", "narrow"), branches, strict=True):
        assert f"AND timeseries_store = '{store}'" in branch
        restored = branch.replace(f"                        AND timeseries_store = '{store}'\n", "")
        if store == "legacy":
            restored = restored.replace("hydro.river_timeseries_legacy", "hydro.river_timeseries")
            expected = original_source
        else:
            assert "hydro.river_timeseries_legacy" not in branch
            expected = re.sub(
                r"              -- transitional compressed-chunk pushdown aid, remove with #1342\n"
                r"              AND ts\.(?:run_id|river_network_version_id|variable) = :\w+\n",
                "", original_source,
            )
        assert restored.strip() == expected.strip()
    assert current.count("ST_AsMVT(tile_rows,") == 1
    assert set(text(current)._bindparams) == set(text(frozen)._bindparams)


def test_national_store_probes_preserve_the_frozen_full_sql_contract() -> None:
    frozen = (Path(__file__).parent / "fixtures/hydro_national_mvt_pre_store_c21bacf9.sql").read_text()
    current = postgis_tile_sql("hydro-national")
    # Only the three already located LATERAL bodies and two candidate columns
    # may change. Compare every byte outside them, not merely selected landmarks.
    pattern = r"(?<=CROSS JOIN LATERAL \()(.*?)(?=\) (?:v|hit)\b)"
    originals = re.findall(pattern, frozen, re.S)
    routed = re.findall(pattern, current, re.S)
    assert len(originals) == len(routed) == 3
    for original, combined in zip(originals, routed, strict=True):
        assert combined.count("UNION ALL") == 1
        assert combined.count("LIMIT 1") == 1
        assert combined.rstrip().endswith("LIMIT 1")
        original_body = original.rsplit("LIMIT 1", 1)[0]
        branches = combined.rsplit("LIMIT 1", 1)[0].split("\nUNION ALL\n")
        for store, branch in zip(("legacy", "narrow"), branches, strict=True):
            assert branch.count(f"AND lr.timeseries_store = '{store}'") == 1
            restored = branch.replace(f"                      AND lr.timeseries_store = '{store}'\n", "")
            if store == "legacy":
                restored = restored.replace("hydro.river_timeseries_legacy", "hydro.river_timeseries")
                expected = original_body
            else:
                assert "hydro.river_timeseries_legacy" not in branch
                expected = re.sub(
                    r"                      -- transitional compressed-chunk pushdown aid, remove with #1342\n"
                    r"                      AND ts\."
                    r"(?:run_id|river_network_version_id|river_segment_id|variable) = [^\n]+\n",
                    "", original_body,
                )
            assert restored.strip() == expected.strip()
    restored_sql = current
    for combined, original in zip(routed, originals, strict=True):
        restored_sql = restored_sql.replace(combined, original, 1)
    assert restored_sql.count(", h.timeseries_store") == 2
    assert restored_sql.replace(", h.timeseries_store", "") == frozen
    assert current.count("ST_AsMVT(tile_rows,") == 1
    assert set(text(current)._bindparams) == set(text(frozen)._bindparams)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, b"pbf-bytes"),
        ({"tile": None, "feature_count": 0}, b""),
        ({"source_identity_count": 0}, (424, "MVT_LIVE_POSTGIS_UNAVAILABLE")),
        ({"invalid_property_count": 1, "invalid_properties": "value"}, (500, "MVT_TILE_CONTRACT_INVALID")),
        ({"feature_count": 10001}, (413, "MVT_TILE_BUDGET_EXCEEDED")),
    ],
)
def test_per_basin_routed_consumer_keeps_one_statement_and_first_row_outcomes(
    monkeypatch: Any, overrides: dict[str, Any], expected: Any,
) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([{**_budget_row(6), **overrides}])
    params = {
        "run_id": "run_a", "basin_version_id": "bv_a", "river_network_version_id": "rn_a",
        "variable": "q_down", "valid_time": datetime(2026, 6, 1, tzinfo=UTC),
    }
    if isinstance(expected, bytes):
        assert hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=398, y=197) == expected
    else:
        with pytest.raises(ApiError) as raised:
            hydro_display._fetch_postgis_tile_bytes(session, "hydro", params, z=9, x=398, y=197)
        assert (raised.value.status_code, raised.value.code) == expected
    assert len(session.executions) == 1
    sql, bound = session.executions[0]
    assert "FROM hydro.river_timeseries_legacy ts" in sql
    assert "FROM hydro.river_timeseries ts" in sql
    assert "AND timeseries_store = 'legacy'" in sql
    assert "AND timeseries_store = 'narrow'" in sql
    assert set(text(sql)._bindparams) <= bound.keys()
    assert all(bound[key] == value for key, value in params.items())
