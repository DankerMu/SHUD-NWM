from __future__ import annotations

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

from apps.api import main
from apps.api.errors import ApiError
from apps.api.routes import hydro_display
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


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _Rows:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, rows: list[dict[str, Any]], dialect: str = "postgresql") -> None:
        self.rows = rows
        self.sql = ""
        self.executions: list[tuple[str, Any]] = []
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))

    def execute(self, statement: Any, _params: Any = None) -> _Rows:
        self.sql = str(statement)
        self.executions.append((self.sql, _params))
        return _Rows(self.rows)

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


def _budget_row(coordinate_count: int) -> dict[str, Any]:
    return {
        "tile": b"pbf-bytes",
        "feature_count": 12,
        "coordinate_count": coordinate_count,
        "source_identity_count": 1,
        "invalid_property_count": 0,
        "invalid_properties": "",
    }


def test_national_river_tile_over_the_shared_limit_but_within_its_own_is_rendered(monkeypatch: Any) -> None:
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    session = _Session([_budget_row(119_999)])

    tile = hydro_display._fetch_postgis_tile_bytes(session, "river-network-national", {}, z=3, x=6, y=3)

    assert tile == b"pbf-bytes"


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

    assert unbound.params == [{"source": None, "cycle": None}]
    assert bound.params == [{"source": "gfs", "cycle": _NATIONAL_CYCLE}]
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
            {"source": "gfs", "cycle": _NATIONAL_CYCLE},
        ),
        ("legacy", _legacy_national_url(), {"source": None, "cycle": None}),
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


def test_layer_catalog_still_digests_every_source_not_one_identity(monkeypatch: Any) -> None:
    """`GET /api/v1/layers` must call the digest helper with NO identity (task 3.1).

    The helper grew keyword-only `source`/`cycle` in this change. The catalog's
    call is the one site that must NOT use them: the catalog advertises the
    legacy source-less template until I5/#2009 moves it, and narrowing its
    digest to one identity would rotate `source_generation` on a schedule that
    has nothing to do with what the catalog describes. Nothing else in the repo
    looks at this call's arguments -- `_default_layer_catalog` is exercised
    directly by `tests/test_api_contract.py`, which passes
    `national_hydro_source_version` in as a literal string and never reaches the
    helper at all.
    """
    recorded: list[dict[str, Any]] = []

    def _recording_digest(_session: Any, **kwargs: Any) -> str:
        recorded.append(kwargs)
        return "national-hydro-digest"

    monkeypatch.setattr(hydro_display, "national_discharge_source_version", _recording_digest)
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: {"run_id": "run_1"})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(
        hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a")
    )
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display, "_default_layer_catalog", lambda *_a, **_k: [])
    # `display_catalog_cached` is a process-wide TTL cache; without this the
    # loader may never run and `recorded` would be empty for the wrong reason.
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())

    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/layers")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    # Non-vacuity: the catalog path really did reach the digest helper once.
    assert len(recorded) == 1, recorded
    assert recorded[0] == {}


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
        selected = []
        for row in self.rows:
            if source is not None and str(row["source_id"]).lower() != source:
                continue
            if cycle is not None and canonical_mvt_time(row["cycle_time"]) != canonical_mvt_time(cycle):
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
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())
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


def test_layer_catalog_is_empty_when_no_run_is_display_ready(monkeypatch: Any) -> None:
    """The other empty state: no ghost discharge entry when nothing is renderable at all."""
    session = _NationalDiscoverySession([])
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _session: None)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())
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
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())
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
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())
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

    `scripts/node27_mvt_prewarm.py` and the frontend's fallback both call it, so
    making `source`/`cycle` mandatory would break them.
    """
    session = _NationalDiscoverySession(_full_coverage_rows(_CYCLE))
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())
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

    def _record(_request: Any, key: str, load: Any) -> Any:
        keys.append(key)
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
