from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime
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
from services.tiles.mvt import (
    MVT_MAX_COORDINATES,
    NATIONAL_DISCHARGE_QUERY_VERSION,
    TileInput,
    TileResponse,
    cache_key,
    layer_metadata,
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
    session = _Session(
        [
            {
                "run_id": "run-a",
                "basin_version_id": "bv-a",
                "river_network_version_id": "rn-a",
                "segment_count": 2,
                "river_sample_count": 8,
                "river_valid_time_start": "2026-07-11T08:00:00Z",
                "river_valid_time_end": "2026-07-11T11:00:00Z",
                "min_lead_time_hours": 0,
                "max_lead_time_hours": 3,
            },
            {
                "run_id": "run-b",
                "basin_version_id": "bv-b",
                "river_network_version_id": "rn-b",
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
        assert "lower(h.source_id) = :source" in site, site_name
        assert "h.cycle_time = :cycle" in site, site_name
        # The NULL guard is what keeps the legacy 5-segment route's run
        # selection unchanged, so it is part of the locked shape. It also splits
        # the two sub-predicates apart, which is why they are located
        # individually and never as one contiguous string.
        assert "CAST(:source AS text) IS NULL OR" in site, site_name
        assert "CAST(:cycle AS timestamptz) IS NULL OR" in site, site_name
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
    assert "CAST(:source AS text) IS NULL OR lower(h.source_id) = :source" in bound.sql
    assert "CAST(:cycle AS timestamptz) IS NULL OR h.cycle_time = :cycle" in bound.sql
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

    def __init__(self) -> None:
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        self.tile_params: list[dict[str, Any]] = []
        self.digest_params: list[dict[str, Any]] = []

    def execute(self, statement: Any, params: Any = None) -> _TileResult:
        if "ST_AsMVT" in str(statement):
            self.tile_params.append(dict(params or {}))
            return _TileResult([dict(self._TILE_ROW)])
        # The only other statement either national route issues is
        # `national_discharge_source_version`'s digest; recording its binds is
        # what lets a case assert the route narrowed it to the requested
        # identity (the tile binds alone cannot see that call at all).
        self.digest_params.append(dict(params or {}))
        return _TileResult([dict(row) for row in self._DIGEST_ROWS])

    def get_bind(self) -> Any:
        return self.bind


class _ExplodingSession:
    """Any use at all is a failure: validation must precede every statement."""

    def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a validation failure must not reach the database")

    def get_bind(self) -> Any:
        raise AssertionError("a validation failure must not reach the database")


def _national_identity_url(source: str, cycle: str, valid_time: str = "2026-09-03T00:00:00Z") -> str:
    return (
        f"{_NATIONAL_ROUTE_PREFIX}/{source}/{cycle}/q_down/{valid_time}"
        f"/{_NATIONAL_TILE_Z}/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"
    )


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
    """`.000Z`, `+00:00`, `+08:00` and `Z` are one instant, one `:cycle` bind, one cache entry.

    `+08:00` is here because the RFC3339 shape gate (#2007 F4) must not narrow
    the accepted spellings to UTC: `2026-09-02T20:00:00+08:00` is the SAME
    instant as the other three and must collapse onto the same canonical
    `2026-09-02T12:00:00Z` bind and cache key, not a fourth one.
    """
    spellings = (
        "2026-09-02T12:00:00.000Z",
        "2026-09-02T12:00:00+00:00",
        "2026-09-02T20:00:00+08:00",
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
    keys: list[str] = []
    for index, (source, cycle) in enumerate(
        (("gfs", "2026-09-02T12:00:00Z"), ("ifs", "2026-09-02T12:00:00Z"), ("gfs", "2026-09-02T00:00:00Z"))
    ):
        response, captured = _request_national_identity_tile(
            _national_identity_url(source, cycle),
            _NationalRouteSession(),
            monkeypatch,
            tmp_path / f"cache-{index}",
        )
        assert response.status_code == 200, response.text
        keys.append(cache_key(captured[0]))

    assert len(set(keys)) == 3, keys


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


def _legacy_national_url(valid_time: str = "2026-09-03T00:00:00Z") -> str:
    return (
        f"{_NATIONAL_ROUTE_PREFIX}/q_down/{valid_time}"
        f"/{_NATIONAL_TILE_Z}/{_NATIONAL_TILE_X}/{_NATIONAL_TILE_Y}.pbf"
    )


@pytest.mark.parametrize(
    ("route_name", "url"),
    [
        ("identity", _national_identity_url("gfs", "2026-09-02T12:00:00Z")),
        ("legacy", _legacy_national_url()),
    ],
)
def test_every_national_tile_sql_bind_is_supplied_by_the_route_that_executes_it(
    route_name: str, url: str, monkeypatch: Any, tmp_path: Any
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
