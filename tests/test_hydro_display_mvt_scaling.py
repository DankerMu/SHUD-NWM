from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from apps.api.routes import hydro_display
from services.tiles.mvt import (
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


class _Session:
    def __init__(self, rows: list[dict[str, Any]], dialect: str = "postgresql") -> None:
        self.rows = rows
        self.sql = ""
        self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))

    def execute(self, statement: Any, _params: Any = None) -> _Rows:
        self.sql = str(statement)
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

    assert national_discharge_source_version(first) != national_discharge_source_version(second)
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

    assert version.startswith("river-network-national:stream-type-aggregate-v2:")
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
    assert "seg.stream_type IS NULL" in hydro_sql


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
