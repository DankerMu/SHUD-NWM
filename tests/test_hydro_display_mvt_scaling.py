from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.api.errors import ApiError
from apps.api.routes import hydro_display
from services.tiles.mvt import (
    MVT_MAX_COORDINATES,
    NATIONAL_DISCHARGE_QUERY_VERSION,
    TileInput,
    TileResponse,
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
