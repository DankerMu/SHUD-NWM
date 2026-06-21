from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import flood_alerts as flood_alert_routes
from apps.api.routes.forecast import get_forecast_store
from packages.common.forecast_store import PsycopgForecastStore
from services.tiles.mvt import MVT_MAX_BYTES, MVT_VALID_TIME_SAMPLE_LIMIT, cache_key, valid_times_for_layer
from workers.flood_frequency.return_period import compute_return_periods

RUN_ID = "fcst_gfs_2026050300_all"
PUBLISHED_RUN_ID = "fcst_gfs_2026050300_published"
DUPLICATE_SEGMENT_RUN_ID = "fcst_gfs_2026050300_duplicate_segments"
DUPLICATE_NETWORK_TIE_RUN_ID = "fcst_gfs_2026050300_duplicate_network_tie"
TIMESTEP_DUPLICATE_RUN_ID = "fcst_gfs_2026050300_timestep_duplicates"
RECOMPUTE_MOVED_PEAK_RUN_ID = "fcst_gfs_2026050300_recompute_moved_peak"
PARTIAL_ROUTE_RUN_ID = "fcst_gfs_2026050300_partial_route"
PARTIAL_ROUTE_WARNING_RUN_ID = "fcst_gfs_2026050300_partial_route_warning"
VALID_TIME_1 = datetime(2026, 5, 3, 6, tzinfo=UTC)
VALID_TIME_2 = datetime(2026, 5, 3, 12, tzinfo=UTC)
VALID_TIME_1_ISO = VALID_TIME_1.isoformat().replace("+00:00", "Z")
VALID_TIME_2_ISO = VALID_TIME_2.isoformat().replace("+00:00", "Z")
RIVER_NETWORK_SOURCE_VERSION_V1 = "river-network-set:f2326d264dd358c8:rnv_v1"
RIVER_NETWORK_SOURCE_VERSION_V1_V2 = "river-network-set:c839aae30c28f855:rnv_v1,rnv_v2"


def test_summary_normal_threshold_and_valid_time() -> None:
    with _client() as client:
        response = client.get(f"/api/v1/flood-alerts/summary?run_id={RUN_ID}")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total_segments"] == 4
        assert data["usable_curves"] == 4
        assert _level_count(data, "normal") == 1
        assert _level_count(data, "watch") == 1
        assert _level_count(data, "high_risk") == 1
        assert _level_count(data, "severe") == 1

        response = client.get(f"/api/v1/flood-alerts/summary?run_id={RUN_ID}&threshold=Q20")
        assert response.status_code == 200
        threshold_data = response.json()["data"]
        assert _level_count(threshold_data, "watch") == 0
        assert _level_count(threshold_data, "high_risk") == 1
        assert _level_count(threshold_data, "severe") == 1

        response = client.get(f"/api/v1/flood-alerts/summary?run_id={RUN_ID}&valid_time={_iso(VALID_TIME_1)}")
        assert response.status_code == 200
        valid_time_data = response.json()["data"]
        assert valid_time_data["total_segments"] == 2
        assert _level_count(valid_time_data, "elevated") == 2
        assert _level_count(valid_time_data, "warning") == 0


def test_summary_counts_duplicate_segment_ids_by_river_network_version() -> None:
    with _client() as client:
        response = client.get(f"/api/v1/flood-alerts/summary?run_id={DUPLICATE_SEGMENT_RUN_ID}")
        assert response.status_code == 200
        data = response.json()["data"]

    assert data["total_segments"] == 2
    assert data["usable_curves"] == 2
    assert data["unavailable_count"] == 0
    assert _level_count(data, "watch") == 1
    assert _level_count(data, "severe") == 1


def test_summary_errors_and_zero_usable_curves() -> None:
    with _client() as client:
        response = client.get("/api/v1/flood-alerts/summary?run_id=missing")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"

        response = client.get("/api/v1/flood-alerts/summary?run_id=run_pending")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "FREQUENCY_NOT_COMPUTED"

        response = client.get("/api/v1/flood-alerts/summary?run_id=run_empty")
        assert response.status_code == 409
        body = response.json()
        assert body["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
        assert body["error"]["details"]["quality_state"] == "unavailable"
        assert body["error"]["details"]["unavailable_products"] == ["return_period_result"]
        assert body["error"]["details"]["return_period_rows"] == 0


def test_ready_status_set_is_explicit_contract() -> None:
    assert flood_alert_routes.FLOOD_PRODUCT_READY_STATUSES == {"frequency_done", "published"}


def test_flood_product_ready_statuses_with_rows_are_readable() -> None:
    for run_id in (RUN_ID, PUBLISHED_RUN_ID):
        with _client() as client:
            response = client.get(f"/api/v1/flood-alerts/summary?run_id={run_id}")
            assert response.status_code == 200
            assert response.json()["data"]["total_segments"] == 4


def test_published_run_rows_are_readable_through_alert_and_map_endpoints() -> None:
    with _client() as client:
        summary = client.get(f"/api/v1/flood-alerts/summary?run_id={PUBLISHED_RUN_ID}")
        ranking = client.get(f"/api/v1/flood-alerts/ranking?run_id={PUBLISHED_RUN_ID}&limit=2")
        segments = client.get(f"/api/v1/flood-alerts/segments?run_id={PUBLISHED_RUN_ID}&min_return_period=20")
        timeline = client.get(
            f"/api/v1/flood-alerts/timeline?run_id={PUBLISHED_RUN_ID}&segment_id=seg_002&river_network_version_id=rnv_v1"
        )
        tile = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id={PUBLISHED_RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )

    for response in (summary, ranking, segments, timeline, tile):
        assert response.status_code == 200
        assert response.json().get("error", {}).get("code") != "FREQUENCY_NOT_COMPUTED"

    assert summary.json()["data"]["total_segments"] == 4
    assert ranking.json()["data"]["items"]
    assert {segment["river_segment_id"] for segment in segments.json()["data"]["segments"]} == {"seg_003", "seg_004"}
    assert timeline.json()["data"]["peak"]["warning_level"] == "warning"
    assert tile.json()["features"]


def test_non_ready_without_rows_is_rejected() -> None:
    with _client() as client:
        response = client.get("/api/v1/flood-alerts/summary?run_id=run_pending")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FREQUENCY_NOT_COMPUTED"


def test_non_ready_with_stray_rows_is_rejected_before_data_query() -> None:
    with _client() as client:
        response = client.get("/api/v1/flood-alerts/summary?run_id=run_stray")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "FREQUENCY_NOT_COMPUTED"
    assert body["error"]["details"]["status"] == "parsed"


def test_warning_thresholds_unavailable_is_not_api_ready() -> None:
    with _client() as client:
        summary = client.get("/api/v1/flood-alerts/summary?run_id=run_warning_unavailable")
        layers = client.get("/api/v1/layers?run_id=run_warning_unavailable")
        tile = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id=run_warning_unavailable&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )

    for response in (summary, layers):
        body = response.json()
        if response is summary:
            assert response.status_code == 409
            assert body["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
            details = body["error"]["details"]
        else:
            assert response.status_code == 200
            flood_layer = next(layer for layer in body["data"] if layer["layer_id"] == "flood-return-period")
            details = flood_layer["metadata"]["product_quality"]
        assert details["quality_state"] == "unavailable"
        assert details["unavailable_products"] == ["warning_thresholds"]
        assert details["return_period_rows"] > 0
        assert details["warning_rows"] == 0
    assert tile.status_code == 409
    tile_body = tile.json()
    assert tile_body["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
    assert tile_body["error"]["details"]["unavailable_products"] == ["warning_thresholds"]
    assert tile_body["error"]["details"]["return_period_rows"] > 0
    assert tile_body["error"]["details"]["warning_rows"] == 0


@pytest.mark.parametrize(
    ("run_id", "unavailable_product", "expected_counts"),
    [
        (
            PARTIAL_ROUTE_RUN_ID,
            "frequency_curves",
            {"result_rows": 2, "return_period_rows": 1, "warning_rows": 1},
        ),
        (
            PARTIAL_ROUTE_WARNING_RUN_ID,
            "warning_thresholds",
            {"result_rows": 2, "return_period_rows": 2, "warning_rows": 2},
        ),
    ],
)
def test_flood_route_partial_product_rows_fail_before_geojson_serialization(
    run_id: str,
    unavailable_product: str,
    expected_counts: dict[str, int],
) -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period?run_id={run_id}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
    details = body["error"]["details"]
    assert details["unavailable_products"] == [unavailable_product]
    assert details[unavailable_product] == "unavailable"
    for field, value in expected_counts.items():
        assert details[field] == value
    assert details["residual_blockers"][0]["code"] in {
        "FREQUENCY_CURVES_UNAVAILABLE",
        "WARNING_THRESHOLDS_UNAVAILABLE",
    }


@pytest.mark.parametrize(
    ("run_id", "unavailable_product", "expected_counts"),
    [
        (
            PARTIAL_ROUTE_RUN_ID,
            "frequency_curves",
            {"result_rows": 2, "return_period_rows": 1, "warning_rows": 1},
        ),
        (
            PARTIAL_ROUTE_WARNING_RUN_ID,
            "warning_thresholds",
            {"result_rows": 2, "return_period_rows": 2, "warning_rows": 2},
        ),
    ],
)
def test_flood_mvt_partial_route_rows_fail_before_cache_lookup_or_live_sql(
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
    unavailable_product: str,
    expected_counts: dict[str, int],
) -> None:
    with _store() as session:
        seed_tile = flood_alert_routes.TileInput(
            layer_id="flood-return-period",
            source_id=run_id,
            source_version=flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, run_id)),
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="duration:1h",
        )
        flood_alert_routes.build_raw_tile_response(session, seed_tile, b"stale-partial-route-cache")

        def fail_cache_lookup(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("flood MVT cache lookup should not run when route product gate fails")

        def fail_live_fetch(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("flood MVT live SQL should not run when route product gate fails")

        monkeypatch.setattr(flood_alert_routes, "read_cached_tile_response", fail_cache_lookup)
        monkeypatch.setattr(flood_alert_routes, "_fetch_flood_mvt_tile_bytes", fail_live_fetch)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(
                    f"/api/v1/tiles/flood-return-period/{run_id}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf"
                )
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
    details = body["error"]["details"]
    assert details["unavailable_products"] == [unavailable_product]
    assert details[unavailable_product] == "unavailable"
    for field, value in expected_counts.items():
        assert details[field] == value


def test_flood_mvt_global_product_gate_fails_before_cache_lookup_or_live_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        seed_tile = flood_alert_routes.TileInput(
            layer_id="flood-return-period",
            source_id="run_empty",
            source_version=flood_alert_routes._run_source_version(
                flood_alert_routes._require_run(session, "run_empty")
            ),
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="duration:1h",
        )
        flood_alert_routes.build_raw_tile_response(session, seed_tile, b"stale-global-cache")

        def fail_cache_lookup(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("flood MVT cache lookup should not run when global product gate fails")

        def fail_live_fetch(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("flood MVT live SQL should not run when global product gate fails")

        monkeypatch.setattr(flood_alert_routes, "read_cached_tile_response", fail_cache_lookup)
        monkeypatch.setattr(flood_alert_routes, "_fetch_flood_mvt_tile_bytes", fail_live_fetch)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(
                    f"/api/v1/tiles/flood-return-period/run_empty/1h/{VALID_TIME_1_ISO}/6/12/24.pbf"
                )
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
    assert body["error"]["details"]["unavailable_products"] == ["return_period_result"]


def test_explicit_all_no_curve_quality_keeps_catalog_discharge_and_blocks_flood_route() -> None:
    run_id = "zz_explicit_no_curve"
    with _store() as session:
        _insert_hydro_run(session, run_id=run_id, cycle_time=datetime(2026, 5, 6, tzinfo=UTC))
        _insert_timeseries_result(session, "seg_001", run_id, VALID_TIME_1, 101.0)
        _write_explicit_flood_quality(
            session,
            run_id=run_id,
            quality_state="unavailable",
            unavailable_products=["frequency_curves", "return_period_result"],
            residual_blockers=[
                {
                    "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                    "state": "unavailable",
                    "quality_flag": "no_frequency_curve",
                    "run_id": run_id,
                    "residual_risk": "No usable frequency curves are available for this run.",
                    "count": 2,
                }
            ],
            expected_result_rows=2,
            meaningful_result_rows=0,
            no_frequency_curve_rows=2,
            no_usable_frequency_curve_rows=0,
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                layers = client.get(f"/api/v1/layers?run_id={run_id}")
                valid_times = client.get(f"/api/v1/layers/flood-return-period/valid-times?run_id={run_id}")
                route = client.get(
                    f"/api/v1/tiles/flood-return-period?run_id={run_id}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
                )
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert layers.status_code == 200
    by_id = {layer["layer_id"]: layer for layer in layers.json()["data"]}
    assert by_id["discharge"]["metadata"]["valid_times"] == [VALID_TIME_1_ISO]
    flood_quality = by_id["flood-return-period"]["metadata"]["product_quality"]
    assert flood_quality["quality_state"] == "unavailable"
    assert flood_quality["unavailable_products"] == ["frequency_curves", "return_period_result"]
    assert flood_quality["expected_result_rows"] == 2
    assert flood_quality["meaningful_result_rows"] == 0
    assert flood_quality["no_frequency_curve_rows"] == 2

    assert valid_times.status_code == 200
    assert valid_times.json()["data"]["valid_times"] == []

    assert route.status_code == 409
    details = route.json()["error"]["details"]
    assert route.json()["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
    assert details["quality_state"] == "unavailable"
    assert details["unavailable_products"] == ["frequency_curves", "return_period_result"]
    assert details["expected_result_rows"] == 2
    assert details["meaningful_result_rows"] == 0
    assert details["no_frequency_curve_rows"] == 2


def test_explicit_partial_quality_preserves_four_two_two_zero_counters_and_blocks_flood_route() -> None:
    run_id = "zz_explicit_partial_quality"
    with _store() as session:
        _insert_hydro_run(session, run_id=run_id, cycle_time=datetime(2026, 5, 6, tzinfo=UTC))
        _insert_timeseries_result(session, "seg_001", run_id, VALID_TIME_1, 101.0)
        _insert_timeseries_result(session, "seg_002", run_id, VALID_TIME_1, 202.0)
        _write_explicit_flood_quality(
            session,
            run_id=run_id,
            quality_state="degraded",
            unavailable_products=["frequency_curves"],
            residual_blockers=[
                {
                    "code": "FREQUENCY_CURVES_UNAVAILABLE",
                    "state": "degraded",
                    "quality_flag": "no_frequency_curve",
                    "run_id": run_id,
                    "residual_risk": "Some result rows have no frequency curve.",
                    "count": 2,
                }
            ],
            expected_result_rows=4,
            meaningful_result_rows=2,
            no_frequency_curve_rows=2,
            no_usable_frequency_curve_rows=0,
            result_rows=4,
            return_period_rows=2,
            warning_rows=2,
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                layers = client.get(f"/api/v1/layers?run_id={run_id}")
                route = client.get(
                    f"/api/v1/tiles/flood-return-period?run_id={run_id}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
                )
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert layers.status_code == 200
    by_id = {layer["layer_id"]: layer for layer in layers.json()["data"]}
    flood_quality = by_id["flood-return-period"]["metadata"]["product_quality"]
    assert flood_quality["quality_state"] == "degraded"
    assert flood_quality["quality_source"] == "explicit"
    assert flood_quality["expected_result_rows"] == 4
    assert flood_quality["meaningful_result_rows"] == 2
    assert flood_quality["no_frequency_curve_rows"] == 2
    assert flood_quality["no_usable_frequency_curve_rows"] == 0

    assert route.status_code == 409
    details = route.json()["error"]["details"]
    assert route.json()["error"]["code"] == "FLOOD_PRODUCT_UNAVAILABLE"
    assert details["quality_state"] == "degraded"
    assert details["frequency_curves"] == "unavailable"
    assert details["expected_result_rows"] == 4
    assert details["meaningful_result_rows"] == 2
    assert details["no_frequency_curve_rows"] == 2
    assert details["no_usable_frequency_curve_rows"] == 0


def test_latest_ready_run_skips_degraded_frequency_products() -> None:
    with _store() as session:
        session.execute(
            text("UPDATE hydro.hydro_run SET status = 'parsed' WHERE run_id <> :run_id"),
            {"run_id": RUN_ID},
        )
        session.execute(
            text("UPDATE hydro.hydro_run SET cycle_time = :cycle_time WHERE run_id = 'run_warning_unavailable'"),
            {"cycle_time": datetime(2026, 5, 4, tzinfo=UTC)},
        )
        session.commit()

        latest = flood_alert_routes.latest_ready_run(session)

    assert latest is not None
    assert latest["run_id"] == RUN_ID


def test_explicit_ready_quality_is_authoritative_for_no_peak_rows() -> None:
    run_id = "zz_no_peak_layer_quality"
    valid_time = datetime(2026, 5, 5, tzinfo=UTC)
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": valid_time,
                "start_time": valid_time,
                "end_time": valid_time + timedelta(hours=1),
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            valid_time,
            100.0,
            2.0,
            "normal",
            False,
            run_id=run_id,
        )
        _write_explicit_flood_quality(
            session,
            run_id=run_id,
            quality_state="ready",
            unavailable_products=[],
            residual_blockers=[],
            expected_result_rows=1,
            meaningful_result_rows=1,
            no_frequency_curve_rows=0,
            no_usable_frequency_curve_rows=0,
            result_rows=1,
            return_period_rows=1,
            warning_rows=1,
        )

        quality = flood_alert_routes._flood_product_quality(session, run_id, status="frequency_done")

    assert quality["quality_state"] == "ready"
    assert quality["max_over_window"] is False
    assert quality["result_rows"] == 1
    assert quality["return_period_rows"] == 1
    assert quality["warning_rows"] == 1


def test_layer_quality_and_latest_ready_sql_use_materialized_quality_without_result_aggregation() -> None:
    route_source = Path(flood_alert_routes.__file__).read_text(encoding="utf-8")
    quality_source = route_source[
        route_source.index("def _flood_product_quality_counts") : route_source.index(
            "def _annotate_flood_layer_quality"
        )
    ]
    mvt_source = Path(inspect.getsourcefile(flood_alert_routes.latest_ready_run) or "").read_text(
        encoding="utf-8"
    )
    latest_source = mvt_source[
        mvt_source.index("def latest_ready_run") : mvt_source.index("def latest_frequency_ready_run")
    ]
    valid_time_source = mvt_source[
        mvt_source.index("def valid_times_for_layer") : mvt_source.index("def _valid_time_discovery")
    ]
    explicit_quality_source = route_source[
        route_source.index("def _explicit_flood_product_quality_row") : route_source.index(
            "def _explicit_flood_quality_from_row"
        )
    ]

    assert "FROM flood.run_product_quality" in explicit_quality_source
    assert "flood.return_period_result" not in explicit_quality_source
    assert "COUNT(*)" not in explicit_quality_source
    assert "SUM(" not in explicit_quality_source
    assert "FROM flood.run_product_quality" in quality_source
    assert "flood.return_period_result" in quality_source
    assert "EXISTS (" in quality_source
    assert "JOIN flood.run_product_quality" in latest_source
    assert "product_quality.quality_state = 'ready'" in latest_source
    assert "FROM flood.return_period_result" in latest_source
    assert "EXISTS (" in latest_source
    assert "GROUP BY run_id" not in latest_source
    assert "COUNT(*)" not in latest_source
    assert "SUM(" not in latest_source
    assert "FROM flood.return_period_result" in valid_time_source
    assert "WHERE run_id = :run_id" in valid_time_source
    assert "GROUP BY" not in valid_time_source


def test_latest_ready_run_uses_explicit_ready_quality() -> None:
    run_id = "zz_no_peak_latest_ready"
    valid_time = datetime(2026, 5, 5, tzinfo=UTC)
    with _store() as session:
        session.execute(text("UPDATE hydro.hydro_run SET status = 'parsed'"))
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": valid_time,
                "start_time": valid_time,
                "end_time": valid_time + timedelta(hours=1),
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            valid_time,
            100.0,
            2.0,
            "normal",
            False,
            run_id=run_id,
        )
        _write_explicit_flood_quality(
            session,
            run_id=run_id,
            quality_state="ready",
            unavailable_products=[],
            residual_blockers=[],
            expected_result_rows=1,
            meaningful_result_rows=1,
            no_frequency_curve_rows=0,
            no_usable_frequency_curve_rows=0,
            result_rows=1,
            return_period_rows=1,
            warning_rows=1,
        )
        session.commit()

        latest = flood_alert_routes.latest_ready_run(session)

    assert latest is not None
    assert latest["run_id"] == run_id


def test_full_ready_explicit_quality_preserves_three_three_zero_zero_counters_through_ready_paths() -> None:
    run_id = "zz_explicit_full_ready_quality"
    valid_time = datetime(2026, 5, 6, tzinfo=UTC)
    with _store() as session:
        session.execute(text("UPDATE hydro.hydro_run SET status = 'parsed'"))
        _insert_hydro_run(session, run_id=run_id, cycle_time=valid_time)
        for index, segment_id in enumerate(("seg_001", "seg_002", "seg_003"), start=1):
            _insert_result(
                session,
                segment_id,
                "basin_v1",
                "rnv_v1",
                valid_time,
                100.0 + index,
                float(index),
                "normal",
                False,
                run_id=run_id,
            )
        _write_explicit_flood_quality(
            session,
            run_id=run_id,
            quality_state="ready",
            unavailable_products=[],
            residual_blockers=[],
            expected_result_rows=3,
            meaningful_result_rows=3,
            no_frequency_curve_rows=0,
            no_usable_frequency_curve_rows=0,
            result_rows=3,
            return_period_rows=3,
            warning_rows=3,
        )
        session.commit()

        latest = flood_alert_routes.latest_ready_run(session)
        quality = flood_alert_routes._flood_product_quality(session, run_id, status="frequency_done")
        route_quality = flood_alert_routes._require_flood_route_product_ready(
            session,
            run_id=run_id,
            duration="1h",
            valid_time=valid_time,
            max_over_window=False,
            status="frequency_done",
        )

    assert latest is not None
    assert latest["run_id"] == run_id
    for returned_quality in (quality, route_quality):
        assert returned_quality["quality_state"] == "ready"
        assert returned_quality["quality_source"] == "explicit"
        assert returned_quality["expected_result_rows"] == 3
        assert returned_quality["meaningful_result_rows"] == 3
        assert returned_quality["no_frequency_curve_rows"] == 0
        assert returned_quality["no_usable_frequency_curve_rows"] == 0
        assert returned_quality["unavailable_products"] == []
        assert returned_quality["residual_blockers"] == []


def test_missing_run_quality_row_fails_closed_for_flood_product_gate() -> None:
    with _store() as session:
        session.execute(
            text("DELETE FROM flood.run_product_quality WHERE run_id = :run_id"),
            {"run_id": RUN_ID},
        )
        session.commit()

        quality = flood_alert_routes._flood_product_quality(session, RUN_ID, status="frequency_done")

    assert quality["quality_state"] == "unavailable"
    assert quality["unavailable_products"] == ["return_period_result"]
    assert quality["result_rows"] == 0


def test_ranking_pagination_basin_filter_and_valid_time() -> None:
    with _client() as client:
        default_response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}")
        assert default_response.status_code == 200
        default_data = default_response.json()["data"]
        assert default_data["limit"] == 10

        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&limit=2&offset=1")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 4
        assert [item["river_segment_id"] for item in data["items"]] == ["seg_004", "seg_002"]
        assert [item["rank"] for item in data["items"]] == [2, 3]
        assert data["items"][0]["geom_centroid"] == {"type": "Point", "coordinates": [113.25, 33.25]}

        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&basin_id=yangtze")
        assert response.status_code == 200
        basin_data = response.json()["data"]
        assert basin_data["total"] == 3
        assert {item["basin_version_id"] for item in basin_data["items"]} == {"basin_v1"}

        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&valid_time={_iso(VALID_TIME_1)}")
        assert response.status_code == 200
        valid_time_data = response.json()["data"]
        assert [item["river_segment_id"] for item in valid_time_data["items"]] == ["seg_002", "seg_001"]
        assert valid_time_data["items"][0]["geom_centroid"] == {"type": "Point", "coordinates": [111.25, 31.25]}


def test_ranking_pagination_orders_duplicate_segment_ties_by_network_version() -> None:
    with _client() as client:
        pages = [
            client.get(f"/api/v1/flood-alerts/ranking?run_id={DUPLICATE_NETWORK_TIE_RUN_ID}&limit=1&offset={offset}")
            for offset in range(2)
        ]

    assert all(page.status_code == 200 for page in pages)
    data = [page.json()["data"] for page in pages]
    assert [page["total"] for page in data] == [2, 2]
    items = [page["items"][0] for page in data]
    assert [item["river_segment_id"] for item in items] == ["dup_seg", "dup_seg"]
    assert [item["river_network_version_id"] for item in items] == ["rnv_v1", "rnv_v2"]
    assert [item["rank"] for item in items] == [1, 2]


def test_recomputed_moved_peak_exposes_one_current_peak_across_alert_views() -> None:
    with _store() as session:
        compute_return_periods(RECOMPUTE_MOVED_PEAK_RUN_ID, session)
        session.execute(
            text(
                """
                UPDATE hydro.river_timeseries
                SET value = CASE
                    WHEN river_segment_id = 'seg_001' AND valid_time = :valid_time_1 THEN 360.0
                    WHEN river_segment_id = 'seg_001' AND valid_time = :valid_time_2 THEN 120.0
                    ELSE value
                END
                WHERE run_id = :run_id
                """
            ),
            {
                "run_id": RECOMPUTE_MOVED_PEAK_RUN_ID,
                "valid_time_1": VALID_TIME_1,
                "valid_time_2": VALID_TIME_2,
            },
        )
        compute_return_periods(RECOMPUTE_MOVED_PEAK_RUN_ID, session)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                summary = client.get(f"/api/v1/flood-alerts/summary?run_id={RECOMPUTE_MOVED_PEAK_RUN_ID}")
                first_page = client.get(f"/api/v1/flood-alerts/ranking?run_id={RECOMPUTE_MOVED_PEAK_RUN_ID}&limit=1")
                second_page = client.get(
                    f"/api/v1/flood-alerts/ranking?run_id={RECOMPUTE_MOVED_PEAK_RUN_ID}&limit=1&offset=1"
                )
                segments = client.get(
                    f"/api/v1/flood-alerts/segments?run_id={RECOMPUTE_MOVED_PEAK_RUN_ID}&limit=10"
                )
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        peak_rows = session.execute(
            text(
                """
                SELECT river_segment_id, valid_time, q_value, warning_level
                FROM flood.return_period_result
                WHERE run_id = :run_id
                  AND max_over_window = true
                ORDER BY river_network_version_id, river_segment_id, valid_time
                """
            ),
            {"run_id": RECOMPUTE_MOVED_PEAK_RUN_ID},
        ).mappings().all()

    assert summary.status_code == first_page.status_code == second_page.status_code == segments.status_code == 200
    assert [(row["river_segment_id"], str(row["valid_time"]), row["q_value"]) for row in peak_rows] == [
        ("seg_001", "2026-05-03 06:00:00+00:00", 360.0),
        ("seg_002", "2026-05-03 06:00:00+00:00", 210.0),
    ]
    summary_data = summary.json()["data"]
    assert summary_data["total_segments"] == 2
    assert summary_data["usable_curves"] == 2
    assert _level_count(summary_data, "severe") == 1
    assert _level_count(summary_data, "normal") == 1
    ranking_pages = [first_page.json()["data"], second_page.json()["data"]]
    assert [page["total"] for page in ranking_pages] == [2, 2]
    assert [page["items"][0]["river_segment_id"] for page in ranking_pages] == ["seg_001", "seg_002"]
    assert [page["items"][0]["valid_time"] for page in ranking_pages] == [_iso(VALID_TIME_1), _iso(VALID_TIME_1)]
    segment_data = segments.json()["data"]
    assert segment_data["total"] == 2
    assert [(segment["river_segment_id"], segment["valid_time"]) for segment in segment_data["segments"]] == [
        ("seg_001", _iso(VALID_TIME_1)),
        ("seg_002", _iso(VALID_TIME_1)),
    ]


def test_ranking_limit_above_contract_uses_validation_envelope() -> None:
    with _client() as client:
        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&limit=201")

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert any(detail["field"] == "query.limit" for detail in body["error"]["details"])


def test_segments_filters_and_empty_result() -> None:
    with _client() as client:
        response = client.get(f"/api/v1/flood-alerts/segments?run_id={RUN_ID}&min_return_period=20")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert data["limit"] == 100
        assert data["offset"] == 0
        assert {segment["river_segment_id"] for segment in data["segments"]} == {"seg_003", "seg_004"}
        assert {segment["river_network_version_id"] for segment in data["segments"]} == {"rnv_v1", "rnv_v2"}
        assert data["segments"][0]["geom_centroid"]["type"] == "Point"

        response = client.get(f"/api/v1/flood-alerts/segments?run_id={RUN_ID}&warning_level=watch,severe")
        assert response.status_code == 200
        level_data = response.json()["data"]
        assert {segment["warning_level"] for segment in level_data["segments"]} == {"watch", "severe"}

        response = client.get(f"/api/v1/flood-alerts/segments?run_id={RUN_ID}&min_return_period=500")
        assert response.status_code == 200
        empty_data = response.json()["data"]
        assert empty_data == {"segments": [], "total": 0, "limit": 100, "offset": 0}


def test_segments_pagination_preserves_total_matching_rows() -> None:
    with _client() as client:
        response = client.get(f"/api/v1/flood-alerts/segments?run_id={RUN_ID}&limit=1&offset=1")
        assert response.status_code == 200
        data = response.json()["data"]

        over_limit = client.get(f"/api/v1/flood-alerts/segments?run_id={RUN_ID}&limit=501")

    assert data["total"] == 4
    assert data["limit"] == 1
    assert data["offset"] == 1
    assert len(data["segments"]) == 1
    assert over_limit.status_code == 422


def test_timeline_normal_with_peak_and_no_frequency_curve() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/flood-alerts/timeline?run_id={RUN_ID}&segment_id=seg_002&river_network_version_id=rnv_v1"
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["river_network_version_id"] == "rnv_v1"
        assert [point["return_period"] for point in data["timesteps"]] == [4.0, 22.0]
        assert data["peak"]["valid_time"] == _iso(VALID_TIME_2)
        assert data["peak"]["warning_level"] == "warning"
        assert data["frequency_thresholds"]["Q20"] == 2900.0

        response = client.get(
            f"/api/v1/flood-alerts/timeline?run_id={RUN_ID}&segment_id=seg_no_curve&river_network_version_id=rnv_v1"
        )
        assert response.status_code == 200
        no_curve = response.json()["data"]
        assert no_curve["frequency_thresholds"] is None
        assert no_curve["quality_note"] == "No frequency curve available for this segment"
        assert no_curve["timesteps"][0]["return_period"] is None


def test_timeline_point_budget_overflow_returns_413_and_keeps_network_binding() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/flood-alerts/timeline?run_id={RUN_ID}&segment_id=seg_002"
            "&river_network_version_id=rnv_v1&max_points=1"
        )

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "FLOOD_ALERT_TIMELINE_POINT_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"max_points": 1}


def test_timeline_filters_duplicate_segment_ids_by_river_network_version() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/flood-alerts/timeline?run_id={DUPLICATE_SEGMENT_RUN_ID}&segment_id=dup_seg&river_network_version_id=rnv_v1"
        )
        assert response.status_code == 200
        rnv_v1 = response.json()["data"]

        response = client.get(
            f"/api/v1/flood-alerts/timeline?run_id={DUPLICATE_SEGMENT_RUN_ID}&segment_id=dup_seg&river_network_version_id=rnv_v2"
        )
        assert response.status_code == 200
        rnv_v2 = response.json()["data"]

    assert rnv_v1["river_network_version_id"] == "rnv_v1"
    assert rnv_v2["river_network_version_id"] == "rnv_v2"
    assert [point["q_value"] for point in rnv_v1["timesteps"]] == [110.0]
    assert [point["q_value"] for point in rnv_v2["timesteps"]] == [220.0]
    assert rnv_v1["peak"]["warning_level"] == "watch"
    assert rnv_v2["peak"]["warning_level"] == "severe"


def test_forecast_series_embeds_frequency_thresholds() -> None:
    store = ThresholdForecastStore()
    app.dependency_overrides[get_forecast_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_002/forecast-series",
                params={"river_network_version_id": "rnv_v1"},
            )
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    assert response.json()["frequency_thresholds"] == {
        "Q2": 1200.0,
        "Q5": 1800.0,
        "Q10": 2300.0,
        "Q20": 2900.0,
        "Q50": 3700.0,
        "Q100": 4500.0,
    }


def test_forecast_series_issue_time_latest_resolves_most_recent_available_issue_time() -> None:
    store = ThresholdForecastStore()
    app.dependency_overrides[get_forecast_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_002/forecast-series",
                params={
                    "river_network_version_id": "rnv_v1",
                    "issue_time": "latest",
                    "variables": "q_down",
                    "scenarios": "GFS",
                },
            )
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    data = response.json()
    assert data["issue_time"] == _iso(VALID_TIME_1)
    assert data["series"][0]["cycle_time"] == _iso(VALID_TIME_1)
    assert data["series"][0]["points"][0][0] == int(VALID_TIME_1.timestamp() * 1000)


def test_flood_tile_returns_json_content_type() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period?run_id={RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")


def test_flood_tile_returns_geojson_feature_collection() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period?run_id={RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "FeatureCollection"
        assert {feature["properties"]["segment_id"] for feature in data["features"]} == {
            "seg_001",
            "seg_002",
        }
        assert data["features"][0]["geometry"]["type"] == "LineString"


def test_flood_tile_feature_properties_complete() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period?run_id={RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )
        assert response.status_code == 200
        features = response.json()["features"]
        assert features
        for feature in features:
            properties = feature["properties"]
            assert set(properties) == {
                "feature_id",
                "segment_id",
                "basin_version_id",
                "river_network_version_id",
                "value",
                "unit",
                "quality_flag",
                "return_period",
                "warning_level",
            }
            assert properties["feature_id"] == f"{properties['river_network_version_id']}::{properties['segment_id']}"
            assert isinstance(properties["segment_id"], str)
            assert properties["basin_version_id"] == "basin_v1"
            assert properties["river_network_version_id"] == "rnv_v1"
            assert isinstance(properties["value"], float)
            assert properties["unit"] == "m3/s"
            assert isinstance(properties["quality_flag"], str)


def test_flood_tile_not_frequency_ready_returns_error_envelope() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period?run_id=run_pending&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )
        assert response.status_code == 409
        assert response.headers["content-type"].startswith("application/json")
        data = response.json()
        assert data["status"] == "error"
        assert data["error"]["code"] == "FREQUENCY_NOT_COMPUTED"


def test_flood_tile_spatial_and_return_period_filters() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id={RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
            "&bbox=109,29,111.75,31.75&return_period=4"
        )
        assert response.status_code == 200
        assert {feature["properties"]["segment_id"] for feature in response.json()["features"]} == {"seg_002"}


def test_flood_tile_duplicate_segment_features_have_network_scoped_identity() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id={DUPLICATE_SEGMENT_RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )

    assert response.status_code == 200
    feature_ids = {feature["properties"]["feature_id"] for feature in response.json()["features"]}
    assert feature_ids == {"rnv_v1::dup_seg", "rnv_v2::dup_seg"}


def test_flood_tile_reads_timestep_row_when_raw_and_peak_share_one_hour_identity() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id={TIMESTEP_DUPLICATE_RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}"
        )

    assert response.status_code == 200
    features = response.json()["features"]
    assert [feature["properties"]["feature_id"] for feature in features] == ["rnv_v1::seg_001"]
    assert features[0]["properties"]["value"] == 123.0
    assert features[0]["properties"]["return_period"] == 6.0
    assert features[0]["properties"]["warning_level"] == "watch"


def test_flood_tile_malformed_bbox_uses_validation_envelope() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period",
            params={
                "run_id": RUN_ID,
                "duration": "1h",
                "valid_time": _iso(VALID_TIME_1),
                "bbox": "109,29,111",
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"] == {"bbox": "109,29,111"}


def test_flood_tile_inverted_bbox_uses_validation_envelope() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period",
            params={
                "run_id": RUN_ID,
                "duration": "1h",
                "valid_time": _iso(VALID_TIME_1),
                "bbox": "112,29,111,31",
            },
        )

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"] == {"bbox": "112,29,111,31"}


def test_flood_tile_non_finite_bbox_uses_validation_envelope() -> None:
    for bbox in ("NaN,29,111,31", "109,29,Infinity,31", "109,-Infinity,111,31"):
        with _client() as client:
            response = client.get(
                "/api/v1/tiles/flood-return-period",
                params={
                    "run_id": RUN_ID,
                    "duration": "1h",
                    "valid_time": _iso(VALID_TIME_1),
                    "bbox": bbox,
                },
            )

        assert response.status_code == 422
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "VALIDATION_ERROR"
        assert body["error"]["details"] == {"bbox": bbox}


def test_flood_tile_feature_budget_overflow_returns_413() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id={RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}&limit=1"
        )

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "FLOOD_RETURN_PERIOD_FEATURE_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"limit": 1}


def test_flood_tile_geojson_coordinate_budget_overflow_returns_413_below_feature_cap() -> None:
    with _client() as client:
        response = client.get(
            "/api/v1/tiles/flood-return-period"
            f"?run_id=run_oversized_geometry&duration=1h&valid_time={_iso(VALID_TIME_1)}&limit=2"
        )

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED"
    assert body["error"]["details"]["limit_type"] == "feature_coordinates"


def test_flood_tile_postgis_geometry_budgets_precede_geojson_serialization() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

    statement = flood_alert_routes._flood_return_period_map_sql(FakeSession(), bbox_filter="")

    assert "WITH matching_segments AS" in statement
    assert "LEFT JOIN core.river_segment rs" in statement
    assert "r.max_over_window = :max_over_window" in statement
    assert "geometry_exclusions AS" in statement
    assert "feature_coordinate_overflow_count" in statement
    assert "dimension_overflow_count" in statement
    assert "malformed_geometry_count" in statement
    assert "null_geometry_count" in statement
    assert "coordinate_count BETWEEN 2 AND :feature_coordinate_limit" in statement
    assert "coordinate_dimensions <= :max_coordinate_dimensions" in statement
    assert "SUM(coordinate_count) OVER" in statement
    assert "running_coordinate_count <= :collection_coordinate_limit" in statement
    assert "true::boolean AS collection_overflow" in statement
    assert "collection_coordinate_count" in statement
    assert "'feature_coordinates'::text AS geometry_limit_type" in statement
    assert "'coordinate_dimensions'::text AS geometry_limit_type" in statement
    assert "'malformed_geometry'::text AS geometry_limit_type" in statement
    assert "'null_geometry'::text AS geometry_limit_type" in statement
    assert "ST_AsGeoJSON(geom)::text AS geom_json" in statement
    assert statement.index("geometry_exclusions AS") < statement.index("ST_AsGeoJSON(geom)::text AS geom_json")
    assert statement.index("running_coordinate_count <= :collection_coordinate_limit") < statement.index(
        "ST_AsGeoJSON(geom)::text AS geom_json"
    )


def test_flood_tile_postgis_union_output_columns_are_explicitly_cast() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

    statement = flood_alert_routes._flood_return_period_map_sql(FakeSession(), bbox_filter="")
    sql = re.sub(r"\s+", " ", statement)

    assert statement.count("UNION ALL") == 5
    assert "NULL AS " not in statement
    assert "'feature_coordinates' AS geometry_limit_type" not in statement
    assert "'coordinate_dimensions' AS geometry_limit_type" not in statement
    assert "'malformed_geometry' AS geometry_limit_type" not in statement
    assert "'null_geometry' AS geometry_limit_type" not in statement

    expected_type_contract = [
        "river_segment_id::text AS river_segment_id",
        "basin_version_id::text AS basin_version_id",
        "river_network_version_id::text AS river_network_version_id",
        "return_period::double precision AS return_period",
        "warning_level::text AS warning_level",
        "q_value::double precision AS q_value",
        "q_unit::text AS q_unit",
        "quality_flag::text AS quality_flag",
        "ST_AsGeoJSON(geom)::text AS geom_json",
        "false::boolean AS collection_overflow",
        "(SELECT collection_coordinate_count FROM overflow)::bigint AS collection_coordinate_count",
        "NULL::text AS geometry_limit_type",
        "NULL::bigint AS geometry_feature_count",
        "NULL::bigint AS geometry_coordinate_count",
        "NULL::integer AS geometry_dimension_count",
        "true::boolean AS collection_overflow",
        "collection_coordinate_count::bigint AS collection_coordinate_count",
        "'feature_coordinates'::text AS geometry_limit_type",
        "feature_coordinate_overflow_count::bigint AS geometry_feature_count",
        "feature_coordinate_count::bigint AS geometry_coordinate_count",
        "'coordinate_dimensions'::text AS geometry_limit_type",
        "dimension_overflow_count::bigint AS geometry_feature_count",
        "dimension_count::integer AS geometry_dimension_count",
        "'malformed_geometry'::text AS geometry_limit_type",
        "malformed_geometry_count::bigint AS geometry_feature_count",
        "malformed_coordinate_count::bigint AS geometry_coordinate_count",
        "'null_geometry'::text AS geometry_limit_type",
        "null_geometry_count::bigint AS geometry_feature_count",
    ]
    for expected in expected_type_contract:
        assert expected in sql

    sentinel_null_columns = [
        "NULL::text AS river_segment_id",
        "NULL::text AS basin_version_id",
        "NULL::text AS river_network_version_id",
        "NULL::double precision AS return_period",
        "NULL::text AS warning_level",
        "NULL::double precision AS q_value",
        "NULL::text AS q_unit",
        "NULL::text AS quality_flag",
        "NULL::text AS geom_json",
    ]
    for expected in sentinel_null_columns:
        assert sql.count(expected) == 5


def test_flood_tile_postgis_collection_overflow_sentinel_returns_413_before_payload_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRows:
        def mappings(self) -> "FakeRows":
            return self

        def __iter__(self) -> Iterator[dict[str, Any]]:
            return iter(
                [
                    {
                        "river_segment_id": None,
                        "basin_version_id": None,
                        "river_network_version_id": None,
                        "return_period": None,
                        "warning_level": None,
                        "q_value": None,
                        "q_unit": None,
                        "quality_flag": None,
                        "geom_json": None,
                        "collection_overflow": True,
                        "collection_coordinate_count": (
                            flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES + 2
                        ),
                    }
                ]
            )

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRows:
            assert "ST_AsGeoJSON(geom)::text AS geom_json" in str(statement)
            assert parameters["max_over_window"] is False
            assert parameters["collection_coordinate_limit"] == (
                flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES
            )
            return FakeRows()

    monkeypatch.setattr(flood_alert_routes, "_require_frequency_ready", lambda _session, _run_id: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)

    with pytest.raises(flood_alert_routes.ApiError) as exc:
        flood_alert_routes.flood_return_period_map(
            run_id=RUN_ID,
            duration="1h",
            valid_time=VALID_TIME_1,
            bbox=None,
            return_period=None,
            limit=10_000,
            session=FakeSession(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413
    assert exc.value.code == "FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED"
    assert exc.value.details["limit_type"] == "collection_coordinates"
    assert exc.value.details["coordinate_count"] == (
        flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES + 2
    )


def test_flood_tile_postgis_feature_geometry_sentinel_returns_413_before_payload_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRows:
        def mappings(self) -> "FakeRows":
            return self

        def __iter__(self) -> Iterator[dict[str, Any]]:
            return iter(
                [
                    {
                        "river_segment_id": None,
                        "basin_version_id": None,
                        "river_network_version_id": None,
                        "return_period": None,
                        "warning_level": None,
                        "q_value": None,
                        "q_unit": None,
                        "quality_flag": None,
                        "geom_json": None,
                        "collection_overflow": False,
                        "collection_coordinate_count": None,
                        "geometry_limit_type": "feature_coordinates",
                        "geometry_feature_count": 1,
                        "geometry_coordinate_count": (
                            flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES + 1
                        ),
                        "geometry_dimension_count": None,
                    }
                ]
            )

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRows:
            sql = str(statement)
            assert "WITH matching_segments AS" in sql
            assert "feature_coordinate_overflow_count" in sql
            assert parameters["max_over_window"] is False
            return FakeRows()

    monkeypatch.setattr(flood_alert_routes, "_require_frequency_ready", lambda _session, _run_id: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)

    with pytest.raises(flood_alert_routes.ApiError) as exc:
        flood_alert_routes.flood_return_period_map(
            run_id=RUN_ID,
            duration="1h",
            valid_time=VALID_TIME_1,
            bbox=None,
            return_period=None,
            limit=10_000,
            session=FakeSession(),  # type: ignore[arg-type]
        )

    assert exc.value.status_code == 413
    assert exc.value.code == "FLOOD_RETURN_PERIOD_GEOJSON_BUDGET_EXCEEDED"
    assert exc.value.details == {
        "limit_type": "feature_coordinates",
        "feature_count": 1,
        "max_coordinates": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES,
        "coordinate_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES + 1,
    }


def test_flood_mvt_canonical_route_returns_protobuf_and_cache_headers() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
        )

    assert response.status_code == 424
    documented_schema = app.openapi()["components"]["responses"]["MvtLivePostgisUnavailable"]["content"][
        "application/json"
    ]["schema"]
    body = response.json()
    assert set(documented_schema["required"]) <= body.keys()
    assert body["status"] in documented_schema["properties"]["status"]["enum"]
    assert body["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert body["error"]["code"] in documented_schema["properties"]["error"]["properties"]["code"]["enum"]
    assert body["error"]["details"]["layer_id"] == "flood-return-period"


def test_flood_mvt_live_postgis_returns_raw_bytes_and_binds_requested_xyz(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self.row = row

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert parameters["run_id"] == RUN_ID
                assert parameters["duration"] == "1h"
                assert parameters["valid_time"] == VALID_TIME_1
                assert parameters["basin_version_id"] == "basin_v1"
                assert parameters["river_network_version_id"] == "rnv_v1"
                assert (parameters["z"], parameters["x"], parameters["y"]) == (6, 12, 24)
                return FakeRowResult({"tile": b"live-tile", "source_identity_count": 1, "source_feature_count": 1})
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS tile test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {
            "run_id": RUN_ID,
            "status": "frequency_done",
            "river_network_version_id": "rnv_v1",
            "basin_version_id": "basin_v1",
            "source_id": "GFS",
            "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 3, 1, tzinfo=UTC),
        },
    )
    monkeypatch.setattr(flood_alert_routes, "_require_flood_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{_iso(VALID_TIME_1)}/6/12/24.pbf"
            )
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-protobuf")
    assert response.content == b"live-tile"
    checksum = hashlib.sha256(b"live-tile").hexdigest()
    assert response.headers["x-tile-checksum"] == checksum
    assert response.headers["etag"] == f'W/"m16-{checksum}"'
    assert response.headers["x-tile-cache"] == "bypass"


def test_station_mvt_live_postgis_disabled_returns_stable_unavailable_error() -> None:
    with _client() as client:
        response = client.get("/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf")

    assert response.status_code == 424
    assert not response.headers["content-type"].startswith("application/x-protobuf")
    body = response.json()
    assert body["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert body["error"]["details"]["layer_id"] == "met-stations"


def test_station_mvt_invalid_identifier_and_xyz_fail_before_station_source_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_source_lookup(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("station source lookup should not run after validation failure")

    monkeypatch.setattr(flood_alert_routes, "_station_source_version", fail_source_lookup)
    with _client() as client:
        invalid_id = client.get("/api/v1/tiles/met-stations/bad%20id/6/12/24.pbf")
        invalid_xyz = client.get("/api/v1/tiles/met-stations/basin_v1/0/1/0.pbf")

    assert invalid_id.status_code == 422
    assert invalid_id.json()["error"]["code"] == "VALIDATION_ERROR"
    assert invalid_xyz.status_code == 422
    assert invalid_xyz.json()["error"]["code"] == "TILE_XYZ_INVALID"


def test_station_mvt_live_postgis_returns_raw_bytes_and_binds_requested_basin_xyz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None) -> None:
            self.row = row
            self.rows = rows if rows is not None else ([row] if row is not None else [])

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

        def all(self) -> list[dict[str, Any]]:
            return self.rows

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "FROM met.met_station" in sql and "ST_AsMVT" not in sql:
                assert parameters["basin_version_id"] == "basin_v1"
                assert parameters["limit"] == flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1
                assert "AND active_flag = true" in re.sub(r"\s+", " ", sql)
                assert "concat_ws" not in sql
                return FakeRowResult(
                    None,
                    [
                        {
                            "station_id": "station_001",
                            "basin_version_id": "basin_v1",
                            "station_name": "Station 001",
                            "station_role": "forcing_proxy",
                            "active_flag": True,
                            "geom": "01010000208A1100000000000000805B400000000000003E40",
                            "created_at": datetime(2026, 5, 3, tzinfo=UTC),
                        },
                        {
                            "station_id": "station_002",
                            "basin_version_id": "basin_v1",
                            "station_name": "Station 002",
                            "station_role": "forcing_grid",
                            "active_flag": True,
                            "geom": "01010000208A1100000000000000C05B400000000000003F40",
                            "created_at": datetime(2026, 5, 3, tzinfo=UTC),
                        },
                    ],
                )
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert parameters["basin_version_id"] == "basin_v1"
                assert (parameters["z"], parameters["x"], parameters["y"]) == (6, 12, 24)
                return FakeRowResult({"tile": b"station-live-tile", "source_identity_count": 1})
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in station live PostGIS tile test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf")
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-protobuf")
    assert response.headers["x-tile-layer-id"] == "met-stations"
    assert response.headers["x-tile-cache"] == "bypass"
    assert response.content == b"station-live-tile"


def test_live_mvt_cache_identity_preserves_valid_time_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)

        first = flood_alert_routes.build_raw_tile_response(
            session,
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
            ),
            b"tile-time-1",
        )
        second = flood_alert_routes.build_raw_tile_response(
            session,
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=_iso(VALID_TIME_2),
                z=6,
                x=12,
                y=24,
            ),
            b"tile-time-2",
        )

        rows = session.execute(text("SELECT cache_key, tile_data FROM map.tile_cache")).mappings().all()

    assert first.cache_key != second.cache_key
    assert {bytes(row["tile_data"]) for row in rows} == {b"tile-time-1", b"tile-time-2"}


@pytest.mark.parametrize(
    "tile",
    [
        flood_alert_routes.TileInput(
            layer_id="flood-return-period",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="duration:1h",
        ),
        flood_alert_routes.TileInput(
            layer_id="discharge",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="variable:q_down",
        ),
    ],
)
def test_mvt_cache_identity_hits_timezone_equivalent_row_valid_time(
    monkeypatch: pytest.MonkeyPatch,
    tile: flood_alert_routes.TileInput,
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        seeded_tile = flood_alert_routes.TileInput(
            layer_id=tile.layer_id,
            source_id=tile.source_id,
            source_version=tile.source_version,
            valid_time="2026-05-18T06:00:00Z",
            z=tile.z,
            x=tile.x,
            y=tile.y,
            style_id=tile.style_id,
            variant_id=tile.variant_id,
            schema_version=tile.schema_version,
            encoder_version=tile.encoder_version,
        )
        seeded = flood_alert_routes.build_raw_tile_response(session, seeded_tile, b"timezone-cached-pbf")
        session.execute(
            text(
                """
                UPDATE map.tile_cache
                SET valid_time = :valid_time
                WHERE cache_key = :cache_key
                """
            ),
            {
                "cache_key": seeded.cache_key,
                "valid_time": datetime(2026, 5, 18, 14, tzinfo=timezone(timedelta(hours=8))),
            },
        )
        session.commit()

        equivalent_tile = flood_alert_routes.TileInput(
            layer_id=tile.layer_id,
            source_id=tile.source_id,
            source_version=tile.source_version,
            valid_time="2026-05-18T06:00:00Z",
            z=tile.z,
            x=tile.x,
            y=tile.y,
            style_id=tile.style_id,
            variant_id=tile.variant_id,
            schema_version=tile.schema_version,
            encoder_version=tile.encoder_version,
        )
        cached = flood_alert_routes.read_cached_tile_response(session, equivalent_tile)

    assert cached is not None
    assert cached.cache_status == "hit"
    assert cached.cache_key == seeded.cache_key
    assert cached.data == b"timezone-cached-pbf"


def test_mvt_cache_identity_canonicalizes_parseable_iso_strings() -> None:
    utc_tile = flood_alert_routes.TileInput(
        layer_id="flood-return-period",
        source_id=RUN_ID,
        source_version="rnv_v1",
        valid_time="2026-05-18T06:00:00Z",
        z=6,
        x=12,
        y=24,
        variant_id="duration:1h",
    )
    offset_tile = flood_alert_routes.TileInput(
        layer_id="flood-return-period",
        source_id=RUN_ID,
        source_version="rnv_v1",
        valid_time="2026-05-18T14:00:00+08:00",
        z=6,
        x=12,
        y=24,
        variant_id="duration:1h",
    )
    non_date_tile = flood_alert_routes.TileInput(
        layer_id="flood-return-period",
        source_id=RUN_ID,
        source_version="rnv_v1",
        valid_time="latest",
        z=6,
        x=12,
        y=24,
        variant_id="duration:1h",
    )

    assert cache_key(utc_tile) == cache_key(offset_tile)
    assert cache_key(non_date_tile) != cache_key(utc_tile)


def test_flood_mvt_cache_identity_preserves_duration_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)

        first = flood_alert_routes.build_raw_tile_response(
            session,
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=_iso(VALID_TIME_1),
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            ),
            b"tile-duration-1h",
        )
        second = flood_alert_routes.build_raw_tile_response(
            session,
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=_iso(VALID_TIME_1),
                z=6,
                x=12,
                y=24,
                variant_id="duration:24h",
            ),
            b"tile-duration-24h",
        )

        rows = session.execute(text("SELECT cache_key, tile_data FROM map.tile_cache")).mappings().all()

    assert first.cache_key != second.cache_key
    assert {bytes(row["tile_data"]) for row in rows} == {b"tile-duration-1h", b"tile-duration-24h"}


@pytest.mark.parametrize(
    ("path", "seed_tile", "fetch_name", "expected_layer_rows"),
    [
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            ),
            "_fetch_flood_mvt_tile_bytes",
            [("flood-return-period", "flood_return_period", "return_period")],
        ),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="discharge",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:q_down",
            ),
            "_fetch_hydro_mvt_tile_bytes",
            [("discharge", "hydrological_output", "q_down")],
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="river-network",
                source_id="basin_v1",
                source_version=RIVER_NETWORK_SOURCE_VERSION_V1,
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
            [("river-network", "river_network", None)],
        ),
        (
            "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="met-stations",
                source_id="basin_v1",
                source_version="met-stations-source-v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_station_mvt_tile_bytes",
            [("met-stations", "meteorological_station", None)],
        ),
    ],
)
def test_live_mvt_route_cache_hit_skips_postgis_fetch(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    seed_tile: flood_alert_routes.TileInput,
    fetch_name: str,
    expected_layer_rows: list[tuple[str, str, str | None]],
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        seeded = flood_alert_routes.build_raw_tile_response(
            session,
            _with_route_source_version(session, seed_tile),
            b"cached-live-tile",
        )

        def fail_if_called(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("live PostGIS fetch should not execute on cache hit")

        monkeypatch.setattr(flood_alert_routes, fetch_name, fail_if_called)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.content == b"cached-live-tile"
    assert response.headers["x-tile-cache"] == "hit"
    assert response.headers["x-tile-cache-key"] == seeded.cache_key
    rows = session.execute(
        text("SELECT layer_id, layer_type, variable FROM map.tile_layer ORDER BY layer_id")
    ).all()
    for expected in expected_layer_rows:
        assert expected in rows


@pytest.mark.parametrize(
    ("path", "seed_tile", "expected_layer"),
    [
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            ),
            "flood-return-period",
        ),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="discharge",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:q_down",
            ),
            "discharge",
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="river-network",
                source_id="basin_v1",
                source_version=RIVER_NETWORK_SOURCE_VERSION_V1,
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "river-network",
        ),
        (
            "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="met-stations",
                source_id="basin_v1",
                source_version="met-stations-source-v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "met-stations",
        ),
    ],
)
def test_seeded_live_mvt_cache_hit_succeeds_with_live_postgis_disabled(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    seed_tile: flood_alert_routes.TileInput,
    expected_layer: str,
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        seeded = flood_alert_routes.build_raw_tile_response(
            session,
            _with_route_source_version(session, seed_tile),
            b"cached-live-tile",
        )
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: False)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.content == b"cached-live-tile"
    assert response.headers["x-tile-cache"] == "hit"
    assert response.headers["x-tile-layer-id"] == expected_layer
    assert response.headers["x-tile-cache-key"] == seeded.cache_key


@pytest.mark.parametrize(
    ("path", "seed_tile", "fetch_name", "invalid_overrides"),
    [
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            ),
            "_fetch_flood_mvt_tile_bytes",
            {"tile_data": b"x" * (MVT_MAX_BYTES + 1)},
        ),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="discharge",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:q_down",
            ),
            "_fetch_hydro_mvt_tile_bytes",
            {"status": "failed"},
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="river-network",
                source_id="basin_v1",
                source_version=RIVER_NETWORK_SOURCE_VERSION_V1,
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
            {"checksum": "not-the-cached-byte-checksum"},
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            ),
            "_fetch_flood_mvt_tile_bytes",
            {"schema_version": "stale-schema"},
        ),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="discharge",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:q_down",
            ),
            "_fetch_hydro_mvt_tile_bytes",
            {"encoder_version": "stale-encoder"},
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="river-network",
                source_id="basin_v1",
                source_version=RIVER_NETWORK_SOURCE_VERSION_V1,
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
            {"source_id": "wrong-source"},
        ),
        (
            "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="met-stations",
                source_id="basin_v1",
                source_version="met-stations-source-v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_station_mvt_tile_bytes",
            {"source_version": "wrong-station-inventory"},
        ),
    ],
)
def test_canonical_mvt_route_invalid_cache_rows_are_misses_without_serving_cached_pbf(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    seed_tile: flood_alert_routes.TileInput,
    fetch_name: str,
    invalid_overrides: dict[str, Any],
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        _seed_mvt_cache_row(
            session,
            _with_route_source_version(session, seed_tile),
            b"invalid-cached-pbf",
            **invalid_overrides,
        )
        monkeypatch.setattr(flood_alert_routes, fetch_name, lambda *_args, **_kwargs: b"fresh-live-pbf")
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(flood_alert_routes.MVT_MEDIA_TYPE)
    assert response.headers["x-tile-cache"] == "miss"
    assert response.content == b"fresh-live-pbf"


@pytest.mark.parametrize(
    ("tile", "fetch_name"),
    [
        (
            flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            ),
            "_fetch_flood_mvt_tile_bytes",
        ),
        (
            flood_alert_routes.TileInput(
                layer_id="discharge",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:q_down",
            ),
            "_fetch_hydro_mvt_tile_bytes",
        ),
        (
            flood_alert_routes.TileInput(
                layer_id="river-network",
                source_id="basin_v1",
                source_version=RIVER_NETWORK_SOURCE_VERSION_V1,
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
        ),
        (
            flood_alert_routes.TileInput(
                layer_id="met-stations",
                source_id="basin_v1",
                source_version="met-stations-source-v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_station_mvt_tile_bytes",
        ),
    ],
)
def test_canonical_mvt_cache_write_upserts_layer_row_then_second_request_hits(
    monkeypatch: pytest.MonkeyPatch,
    tile: flood_alert_routes.TileInput,
    fetch_name: str,
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        first = flood_alert_routes.build_raw_tile_response(session, tile, b"fresh-live-tile")
        layer_row = session.execute(
            text("SELECT layer_id, tile_format FROM map.tile_layer WHERE layer_id = :layer_id"),
            {"layer_id": tile.layer_id},
        ).one()

        def fail_if_called(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("live fetch should not execute after cache write")

        monkeypatch.setattr(flood_alert_routes, fetch_name, fail_if_called)
        cached = flood_alert_routes.read_cached_tile_response(session, tile)

    assert first.cache_status == "miss"
    assert layer_row == (tile.layer_id, "mvt")
    assert cached is not None
    assert cached.cache_status == "hit"
    assert cached.data == b"fresh-live-tile"


@pytest.mark.parametrize(
    ("path", "fetch_name", "layer_id"),
    [
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
            "flood-return-period",
        ),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_hydro_mvt_tile_bytes",
            "discharge",
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            "_fetch_river_network_mvt_tile_bytes",
            "river-network",
        ),
        (
            "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf",
            "_fetch_station_mvt_tile_bytes",
            "met-stations",
        ),
    ],
)
def test_canonical_mvt_route_first_request_writes_fk_backed_cache_then_second_hits(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    fetch_name: str,
    layer_id: str,
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(flood_alert_routes, fetch_name, lambda *_args, **_kwargs: b"fresh-route-tile")
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)

                def fail_if_called(*_args: Any, **_kwargs: Any) -> bytes:
                    raise AssertionError("live fetch should not execute on route cache hit")

                monkeypatch.setattr(flood_alert_routes, fetch_name, fail_if_called)
                second = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        layer_count = session.execute(
            text("SELECT COUNT(*) FROM map.tile_layer WHERE layer_id = :layer_id"),
            {"layer_id": layer_id},
        ).scalar_one()
        cache_count = session.execute(
            text("SELECT COUNT(*) FROM map.tile_cache WHERE layer_id = :layer_id"),
            {"layer_id": layer_id},
        ).scalar_one()

    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"fresh-route-tile"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "hit"
    assert second.content == b"fresh-route-tile"
    assert layer_count == 1
    assert cache_count == 1


def test_river_network_cache_identity_changes_with_river_network_version_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf"
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_river_network_mvt_tile_bytes",
            lambda *_args, **_kwargs: b"rnv-a",
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)
                first_key = first.headers["x-tile-cache-key"]

                session.execute(
                    text(
                        """
                        UPDATE core.river_network_version
                        SET basin_version_id = 'basin_v1'
                        WHERE river_network_version_id = 'rnv_v2'
                        """
                    )
                )
                session.commit()
                monkeypatch.setattr(
                    flood_alert_routes,
                    "_fetch_river_network_mvt_tile_bytes",
                    lambda *_args, **_kwargs: b"rnv-a-plus-b",
                )
                second = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        cache_rows = session.execute(
            text(
                """
                SELECT cache_key, source_id, source_version, tile_data
                FROM map.tile_cache
                WHERE layer_id = 'river-network'
                ORDER BY created_at, cache_key
                """
            )
        ).mappings().all()
        layer_source_version = session.execute(
            text("SELECT source_product_id, source_version FROM map.tile_layer WHERE layer_id = 'river-network'")
        ).one()

    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"rnv-a"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "miss"
    assert second.content == b"rnv-a-plus-b"
    assert second.headers["x-tile-cache-key"] != first_key
    assert [row["source_id"] for row in cache_rows] == ["basin_v1", "basin_v1"]
    assert {row["source_version"] for row in cache_rows} == {
        RIVER_NETWORK_SOURCE_VERSION_V1,
        RIVER_NETWORK_SOURCE_VERSION_V1_V2,
    }
    assert {bytes(row["tile_data"]) for row in cache_rows} == {b"rnv-a", b"rnv-a-plus-b"}
    assert layer_source_version == ("basin_v1", RIVER_NETWORK_SOURCE_VERSION_V1_V2)


def test_station_mvt_cache_identity_changes_when_station_inventory_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf"
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_station_mvt_tile_bytes",
            lambda *_args, **_kwargs: b"station-tile-v1",
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)
                first_key = first.headers["x-tile-cache-key"]

                session.execute(
                    text(
                        """
                        UPDATE met.met_station
                        SET station_name = 'Station 001 renamed'
                        WHERE station_id = 'station_001'
                        """
                    )
                )
                session.commit()
                monkeypatch.setattr(
                    flood_alert_routes,
                    "_fetch_station_mvt_tile_bytes",
                    lambda *_args, **_kwargs: b"station-tile-v2",
                )
                second = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        cache_rows = session.execute(
            text(
                """
                SELECT cache_key, source_id, source_version, tile_data
                FROM map.tile_cache
                WHERE layer_id = 'met-stations'
                ORDER BY created_at, cache_key
                """
            )
        ).mappings().all()
        layer_source_version = session.execute(
            text("SELECT source_product_id, source_version FROM map.tile_layer WHERE layer_id = 'met-stations'")
        ).one()

    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"station-tile-v1"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "miss"
    assert second.content == b"station-tile-v2"
    assert second.headers["x-tile-cache-key"] != first_key
    assert [row["source_id"] for row in cache_rows] == ["basin_v1", "basin_v1"]
    assert len({row["source_version"] for row in cache_rows}) == 2
    assert {bytes(row["tile_data"]) for row in cache_rows} == {b"station-tile-v1", b"station-tile-v2"}
    assert layer_source_version[0] == "basin_v1"
    second_cache_row = next(row for row in cache_rows if row["cache_key"] == second.headers["x-tile-cache-key"])
    assert layer_source_version[1] == second_cache_row["source_version"]


def test_station_mvt_delimiter_collision_pair_changes_source_version_and_cache_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf"
    with _store() as session:
        session.execute(
            text(
                """
                UPDATE met.met_station
                SET active_flag = CASE WHEN station_id = 'station_001' THEN 1 ELSE 0 END,
                    station_name = CASE WHEN station_id = 'station_001' THEN 'A|B' ELSE station_name END,
                    station_role = CASE WHEN station_id = 'station_001' THEN 'C' ELSE station_role END
                WHERE basin_version_id = 'basin_v1'
                """
            )
        )
        session.commit()
        first_source_version = flood_alert_routes._station_source_version(session, "basin_v1")

        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_station_mvt_tile_bytes",
            lambda *_args, **_kwargs: b"delimiter-collision-before",
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)
                first_key = first.headers["x-tile-cache-key"]

                session.execute(
                    text(
                        """
                        UPDATE met.met_station
                        SET station_name = 'A',
                            station_role = 'B|C'
                        WHERE station_id = 'station_001'
                          AND basin_version_id = 'basin_v1'
                        """
                    )
                )
                session.commit()
                second_source_version = flood_alert_routes._station_source_version(session, "basin_v1")
                monkeypatch.setattr(
                    flood_alert_routes,
                    "_fetch_station_mvt_tile_bytes",
                    lambda *_args, **_kwargs: b"delimiter-collision-after",
                )
                second = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        cache_rows = session.execute(
            text(
                """
                SELECT cache_key, source_version, tile_data
                FROM map.tile_cache
                WHERE layer_id = 'met-stations'
                ORDER BY created_at, cache_key
                """
            )
        ).mappings().all()

    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"delimiter-collision-before"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "miss"
    assert second.content == b"delimiter-collision-after"
    assert first_source_version != second_source_version
    assert first_source_version.endswith(":basin_v1:1:station_001:station_001")
    assert second_source_version.endswith(":basin_v1:1:station_001:station_001")
    assert second.headers["x-tile-cache-key"] != first_key
    assert {row["source_version"] for row in cache_rows} == {first_source_version, second_source_version}
    assert {bytes(row["tile_data"]) for row in cache_rows} == {
        b"delimiter-collision-before",
        b"delimiter-collision-after",
    }


def test_station_mvt_cache_identity_ignores_inactive_station_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = "/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf"
    with _store() as session:
        session.execute(
            text(
                """
                UPDATE met.met_station
                SET active_flag = 0
                WHERE station_id = 'station_002'
                """
            )
        )
        session.commit()
        first_source_version = flood_alert_routes._station_source_version(session, "basin_v1")

        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_station_mvt_tile_bytes",
            lambda *_args, **_kwargs: b"active-only-station-tile",
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)
                first_key = first.headers["x-tile-cache-key"]

                session.execute(
                    text(
                        """
                        UPDATE met.met_station
                        SET station_name = 'Inactive renamed',
                            geom = 'POINT(119 39)',
                            created_at = :created_at
                        WHERE station_id = 'station_002'
                        """
                    ),
                    {"created_at": datetime(2026, 5, 4, tzinfo=UTC)},
                )
                session.commit()
                second_source_version = flood_alert_routes._station_source_version(session, "basin_v1")

                def fail_if_called(*_args: Any, **_kwargs: Any) -> bytes:
                    raise AssertionError("inactive-only station changes should not miss the active cache key")

                monkeypatch.setattr(flood_alert_routes, "_fetch_station_mvt_tile_bytes", fail_if_called)
                second = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        cache_rows = session.execute(
            text(
                """
                SELECT cache_key, source_version, tile_data
                FROM map.tile_cache
                WHERE layer_id = 'met-stations'
                ORDER BY cache_key
                """
            )
        ).mappings().all()

    assert first_source_version.endswith(":basin_v1:1:station_001:station_001")
    assert first_source_version == second_source_version
    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"active-only-station-tile"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "hit"
    assert second.headers["x-tile-cache-key"] == first_key
    assert second.content == b"active-only-station-tile"
    assert len(cache_rows) == 1
    assert cache_rows[0]["source_version"] == first_source_version
    assert bytes(cache_rows[0]["tile_data"]) == b"active-only-station-tile"


def test_flood_mvt_cache_identity_changes_when_run_updated_at_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf"
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_flood_mvt_tile_bytes",
            lambda *_args, **_kwargs: b"flood-tile-v1",
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)
                first_key = first.headers["x-tile-cache-key"]
                first_source_version = session.execute(
                    text("SELECT source_version FROM map.tile_cache WHERE cache_key = :cache_key"),
                    {"cache_key": first_key},
                ).scalar_one()

                session.execute(
                    text(
                        """
                        UPDATE hydro.hydro_run
                        SET updated_at = :updated_at
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": RUN_ID, "updated_at": datetime(2026, 5, 3, 2, tzinfo=UTC)},
                )
                session.commit()
                monkeypatch.setattr(
                    flood_alert_routes,
                    "_fetch_flood_mvt_tile_bytes",
                    lambda *_args, **_kwargs: b"flood-tile-v2",
                )
                second = client.get(path)
                second_source_version = session.execute(
                    text("SELECT source_version FROM map.tile_cache WHERE cache_key = :cache_key"),
                    {"cache_key": second.headers["x-tile-cache-key"]},
                ).scalar_one()
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        cache_rows = session.execute(
            text(
                """
                SELECT cache_key, source_version, tile_data
                FROM map.tile_cache
                WHERE layer_id = 'flood-return-period'
                ORDER BY cache_key
                """
            )
        ).mappings().all()

    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"flood-tile-v1"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "miss"
    assert second.content == b"flood-tile-v2"
    assert second.headers["x-tile-cache-key"] != first_key
    assert first_source_version != second_source_version
    assert all("run-revision:" in row["source_version"] for row in cache_rows)
    assert {bytes(row["tile_data"]) for row in cache_rows} == {b"flood-tile-v1", b"flood-tile-v2"}


def test_hydro_mvt_cache_identity_changes_when_run_updated_at_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf"
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_hydro_mvt_tile_bytes",
            lambda *_args, **_kwargs: b"hydro-tile-v1",
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                first = client.get(path)
                first_key = first.headers["x-tile-cache-key"]
                first_source_version = session.execute(
                    text("SELECT source_version FROM map.tile_cache WHERE cache_key = :cache_key"),
                    {"cache_key": first_key},
                ).scalar_one()

                session.execute(
                    text(
                        """
                        UPDATE hydro.hydro_run
                        SET updated_at = :updated_at
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": RUN_ID, "updated_at": datetime(2026, 5, 3, 3, tzinfo=UTC)},
                )
                session.commit()
                monkeypatch.setattr(
                    flood_alert_routes,
                    "_fetch_hydro_mvt_tile_bytes",
                    lambda *_args, **_kwargs: b"hydro-tile-v2",
                )
                second = client.get(path)
                second_source_version = session.execute(
                    text("SELECT source_version FROM map.tile_cache WHERE cache_key = :cache_key"),
                    {"cache_key": second.headers["x-tile-cache-key"]},
                ).scalar_one()
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

        cache_rows = session.execute(
            text(
                """
                SELECT cache_key, source_version, tile_data
                FROM map.tile_cache
                WHERE layer_id = 'discharge'
                ORDER BY cache_key
                """
            )
        ).mappings().all()

    assert first.status_code == 200
    assert first.headers["x-tile-cache"] == "miss"
    assert first.content == b"hydro-tile-v1"
    assert second.status_code == 200
    assert second.headers["x-tile-cache"] == "miss"
    assert second.content == b"hydro-tile-v2"
    assert second.headers["x-tile-cache-key"] != first_key
    assert first_source_version != second_source_version
    assert all("run-revision:" in row["source_version"] for row in cache_rows)
    assert {bytes(row["tile_data"]) for row in cache_rows} == {b"hydro-tile-v1", b"hydro-tile-v2"}


def test_layer_metadata_cache_identity_changes_when_run_updated_at_changes() -> None:
    with _store() as session:
        source_version = flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, RUN_ID))
        old_metadata = {
            layer.layer_id: layer.metadata or {}
            for layer in flood_alert_routes._default_layer_catalog(
                session,
                run_id=RUN_ID,
                source_version=source_version,
                basin_version_id="basin_v1",
                river_network_version_id="rnv_v1",
            )
        }
        session.execute(
            text("UPDATE hydro.hydro_run SET updated_at = :updated_at WHERE run_id = :run_id"),
            {"run_id": RUN_ID, "updated_at": datetime(2026, 5, 3, 4, tzinfo=UTC)},
        )
        session.commit()
        source_version = flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, RUN_ID))
        new_metadata = {
            layer.layer_id: layer.metadata or {}
            for layer in flood_alert_routes._default_layer_catalog(
                session,
                run_id=RUN_ID,
                source_version=source_version,
                basin_version_id="basin_v1",
                river_network_version_id="rnv_v1",
            )
        }

    # Flood / warning layers stay per-run, so their cache identity must rotate when the
    # hydro run's updated_at changes (drives source_version → cache_version → cache_etag).
    for layer_id in ("flood-return-period", "warning-level"):
        assert old_metadata[layer_id]["source_refs"]["run_id"] == RUN_ID
        assert old_metadata[layer_id]["source_refs"]["basin_version_id"] == "basin_v1"
        assert old_metadata[layer_id]["source_refs"]["river_network_version_id"] == "rnv_v1"
        assert (
            old_metadata[layer_id]["source_refs"]["source_version"]
            != new_metadata[layer_id]["source_refs"]["source_version"]
        )
        assert old_metadata[layer_id]["cache_version"] != new_metadata[layer_id]["cache_version"]
        assert old_metadata[layer_id]["cache_etag"] != new_metadata[layer_id]["cache_etag"]

    # Discharge is national (spec invariant *Default discharge tile URL is national across all
    # /api/v1/layers callers*; *Discharge catalog cache identity is run-agnostic*): source_refs is
    # always empty and cache identity does NOT rotate with the run's updated_at (it rotates only
    # when national_discharge_valid_times changes, i.e. a new ready run lands).
    assert old_metadata["discharge"]["source_refs"] == {}
    assert new_metadata["discharge"]["source_refs"] == {}
    assert old_metadata["discharge"]["cache_version"] == new_metadata["discharge"]["cache_version"]
    assert old_metadata["discharge"]["cache_etag"] == new_metadata["discharge"]["cache_etag"]


def test_layers_catalog_discharge_always_national() -> None:
    """Spec invariant (overview-data-contracts: *Default discharge tile URL is national across all
    `/api/v1/layers` callers* — scenarios *Run-scoped `/api/v1/layers?run_id=<X>` catalog* +
    *Frontend enrichment phase does not downgrade discharge*): even when the caller passes a concrete
    `run_id` (simulating frontend enrichment `fetchLayers(latestRun.run_id)`), the discharge entry's
    tile URL template MUST be the national one (no `{run_id}` placeholder). Issue #601 root cause.
    """
    with _store() as session:
        source_version = flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, RUN_ID))
        catalog = {
            layer.layer_id: layer.metadata or {}
            for layer in flood_alert_routes._default_layer_catalog(
                session,
                run_id=RUN_ID,
                source_version=source_version,
                basin_version_id="basin_v1",
                river_network_version_id="rnv_v1",
                national=False,
            )
        }

    discharge = catalog["discharge"]
    assert discharge["tile_url_template"] == "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    assert "{run_id}" not in discharge["tile_url_template"]
    assert discharge["required_placeholders"] == ["valid_time", "z", "x", "y"]
    assert "run_id" not in discharge["required_placeholders"]
    assert discharge["maplibre_source_layer"] == "hydro"
    # spec phrasing "metadata.properties" maps to `property_schema.required` in the implementation
    # (see `services/tiles/mvt.py` `layer_metadata` return shape: line 936).
    assert "basin_id" in discharge["property_schema"]["required"]


def test_layers_catalog_flood_warning_remain_run_scoped() -> None:
    """Spec invariant (overview-data-contracts: *Flood-return-period and warning-level remain
    run-scoped*): the discharge-specific national fix MUST NOT regress flood-return-period /
    warning-level (still per-run) or river-network (still basin-scoped) templates.
    """
    with _store() as session:
        source_version = flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, RUN_ID))
        catalog = {
            layer.layer_id: layer.metadata or {}
            for layer in flood_alert_routes._default_layer_catalog(
                session,
                run_id=RUN_ID,
                source_version=source_version,
                basin_version_id="basin_v1",
                river_network_version_id="rnv_v1",
                national=False,
            )
        }

    for layer_id in ("flood-return-period", "warning-level"):
        assert "{run_id}" in catalog[layer_id]["tile_url_template"]

    river_network = catalog["river-network"]
    assert river_network["tile_url_template"] == "/api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf"
    assert river_network["required_placeholders"] == ["basin_version_id", "z", "x", "y"]


def test_layers_catalog_discharge_cache_identity_run_agnostic() -> None:
    """Spec invariant (overview-data-contracts: *Discharge catalog cache identity is run-agnostic* +
    *Runless `/api/v1/layers` catalog*): the discharge entry's per-layer ETag MUST be byte-identical
    between the runless (`national=True, run_id=None`) call and the run-scoped (`national=False,
    run_id=RUN_ID`) call, with `source_refs == {}` in both, so the CDN need not partition the
    discharge entry on run_id. Additionally pins the runless contract: national template + no
    run_id placeholder + maplibre_source_layer='hydro' + basin_id in properties.
    """
    with _store() as session:
        source_version = flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, RUN_ID))
        runless = {
            layer.layer_id: layer.metadata or {}
            for layer in flood_alert_routes._default_layer_catalog(
                session,
                run_id=None,
                source_version=None,
                basin_version_id=None,
                river_network_version_id=None,
                national=True,
            )
        }
        run_scoped = {
            layer.layer_id: layer.metadata or {}
            for layer in flood_alert_routes._default_layer_catalog(
                session,
                run_id=RUN_ID,
                source_version=source_version,
                basin_version_id="basin_v1",
                river_network_version_id="rnv_v1",
                national=False,
            )
        }

    runless_discharge = runless["discharge"]
    run_scoped_discharge = run_scoped["discharge"]

    # Cache identity: source_refs empty + version byte-identical across both call shapes.
    assert runless_discharge["source_refs"] == {}
    assert run_scoped_discharge["source_refs"] == {}
    assert isinstance(runless_discharge["cache_version"], str)
    assert runless_discharge["cache_version"] == run_scoped_discharge["cache_version"]
    assert runless_discharge["cache_etag"] == run_scoped_discharge["cache_etag"]

    # Runless invariants explicitly pinned (spec scenario *Runless `/api/v1/layers` catalog*).
    assert runless_discharge["tile_url_template"] == "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    assert runless_discharge["required_placeholders"] == ["valid_time", "z", "x", "y"]
    assert runless_discharge["maplibre_source_layer"] == "hydro"
    # spec phrasing "metadata.properties" maps to `property_schema.required` in the implementation.
    assert "basin_id" in runless_discharge["property_schema"]["required"]


def test_mvt_cache_fixture_enforces_tile_layer_fk() -> None:
    with _store() as session:
        with pytest.raises(SQLAlchemyError):
            session.execute(
                text(
                    """
                    INSERT INTO map.tile_cache (layer_id, z, x, y, tile_data, tile_uri, cache_key, etag)
                    VALUES ('missing-layer', 6, 12, 24, :tile_data, 'missing', 'missing', 'etag')
                    """
                ),
                {"tile_data": b"orphan"},
            )
            session.commit()
        session.rollback()


def test_live_mvt_cache_write_failure_returns_tile_with_bypass_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def first(self) -> None:
            raise RuntimeError("not used")

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, *_args: Any, **_kwargs: Any) -> FakeRowResult:
            raise SQLAlchemyError("cache unavailable")

        def rollback(self) -> None:
            return None

    tile = flood_alert_routes.build_raw_tile_response(
        FakeSession(),  # type: ignore[arg-type]
        flood_alert_routes.TileInput(
            layer_id="flood-return-period",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=_iso(VALID_TIME_1),
            z=6,
            x=12,
            y=24,
        ),
        b"live-tile",
    )

    assert tile.data == b"live-tile"
    assert tile.cache_status == "bypass"


def test_tile_domain_error_maps_to_public_api_error_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_raw_tile_build(*_args: Any, **_kwargs: Any) -> object:
        raise flood_alert_routes.TileError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Raw MVT tile payload exceeded the configured byte budget.",
            details={
                "max_bytes": MVT_MAX_BYTES,
                "payload_bytes": MVT_MAX_BYTES + 1,
                "layer_id": "flood-return-period",
            },
        )

    monkeypatch.setattr(flood_alert_routes, "_require_live_postgis_mvt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_fetch_flood_mvt_tile_bytes", lambda *_args, **_kwargs: b"tile")
    monkeypatch.setattr(flood_alert_routes, "_build_raw_tile_response", fail_raw_tile_build)

    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{_iso(VALID_TIME_1)}/6/12/24.pbf",
        )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["status"] == "error"
    assert body["error"] == {
        "code": "MVT_TILE_BUDGET_EXCEEDED",
        "message": "Raw MVT tile payload exceeded the configured byte budget.",
        "details": {
            "max_bytes": MVT_MAX_BYTES,
            "payload_bytes": MVT_MAX_BYTES + 1,
            "layer_id": "flood-return-period",
        },
    }


@pytest.mark.parametrize(
    ("path", "expected_layer_id", "expected_sql_layer", "expected_params"),
    [
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "flood-return-period",
            "flood-return-period",
            {
                "run_id": RUN_ID,
                "duration": "1h",
                "valid_time": VALID_TIME_1,
                "basin_version_id": "basin_v1",
                "river_network_version_id": "rnv_v1",
            },
        ),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "discharge",
            "hydro",
            {
                "run_id": RUN_ID,
                "variable": "q_down",
                "valid_time": VALID_TIME_1,
                "basin_version_id": "basin_v1",
                "river_network_version_id": "rnv_v1",
            },
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            "river-network",
            "river-network",
            {"basin_version_id": "basin_v1"},
        ),
    ],
)
def test_live_mvt_zero_feature_tile_returns_pbf_and_cache_headers(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_layer_id: str,
    expected_sql_layer: str,
    expected_params: dict[str, Any],
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None) -> None:
            self.row = row
            self.rows = rows if rows is not None else ([row] if row is not None else [])

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

        def all(self) -> list[dict[str, Any]]:
            return self.rows

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "FROM hydro.river_timeseries" in sql and "LIMIT 1" in sql:
                assert parameters["run_id"] == RUN_ID
                assert parameters["variable"] == "q_down"
                assert parameters["valid_time"] == VALID_TIME_1
                assert parameters["basin_version_id"] == "basin_v1"
                assert parameters["river_network_version_id"] == "rnv_v1"
                return FakeRowResult({"exists": 1})
            if "FROM flood.return_period_result" in sql and "LIMIT 1" in sql:
                assert parameters["run_id"] == RUN_ID
                assert parameters["duration"] == "1h"
                assert parameters["valid_time"] == VALID_TIME_1
                assert parameters["basin_version_id"] == "basin_v1"
                assert parameters["river_network_version_id"] == "rnv_v1"
                return FakeRowResult({"exists": 1})
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert f"'{expected_sql_layer.replace('-', '_')}'" in sql or expected_sql_layer == "hydro"
                for key, value in expected_params.items():
                    assert parameters[key] == value
                return FakeRowResult(
                    {
                        "tile": b"",
                        "source_identity_count": 1,
                        "source_feature_count": 0,
                        "feature_count": 0,
                        "coordinate_count": 0,
                        "feature_coordinate_overflow_count": 0,
                        "feature_coordinate_count": 0,
                        "coordinate_dimension_overflow_count": 0,
                        "coordinate_dimension_count": 0,
                        "invalid_property_count": 0,
                        "invalid_properties": "",
                    }
                )
            if "SELECT DISTINCT river_network_version_id" in sql:
                assert parameters["basin_version_id"] == "basin_v1"
                return FakeRowResult(
                    {"river_network_version_id": "rnv_v1"},
                    [{"river_network_version_id": "rnv_v1"}],
                )
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS zero-feature test: {sql}")

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_run",
        lambda _session, _run_id: {
            "run_id": RUN_ID,
            "status": "completed",
            "river_network_version_id": "rnv_v1",
            "basin_version_id": "basin_v1",
            "source_id": "GFS",
            "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 3, 1, tzinfo=UTC),
        },
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {
            "run_id": RUN_ID,
            "status": "frequency_done",
            "river_network_version_id": "rnv_v1",
            "basin_version_id": "basin_v1",
            "source_id": "GFS",
            "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 3, 1, tzinfo=UTC),
        },
    )
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(path)
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == flood_alert_routes.MVT_MEDIA_TYPE
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-tile-layer-id"] == expected_layer_id
    assert response.headers["x-tile-cache"] == "bypass"
    assert response.headers["x-mvt-schema-version"] == flood_alert_routes.MVT_SCHEMA_VERSION
    assert response.content == b""


def test_live_mvt_missing_source_identity_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self.row = row

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert parameters["run_id"] == RUN_ID
                assert parameters["variable"] == "q_down"
                assert parameters["valid_time"] == VALID_TIME_1
                return FakeRowResult(
                    {
                        "tile": b"",
                        "source_identity_count": 0,
                        "source_feature_count": 1,
                        "feature_count": 0,
                        "coordinate_count": 0,
                        "feature_coordinate_overflow_count": 0,
                        "feature_coordinate_count": 0,
                        "coordinate_dimension_overflow_count": 0,
                        "coordinate_dimension_count": 0,
                        "invalid_property_count": 0,
                        "invalid_properties": "",
                    }
                )
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS missing-identity test: {sql}")

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(flood_alert_routes, "_require_hydro_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{_iso(VALID_TIME_1)}/6/12/24.pbf"
            )
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 424
    body = response.json()
    assert body["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert body["error"]["details"]["layer_id"] == "discharge"


def test_station_mvt_active_empty_source_identity_fails_before_cache_or_live_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        session.execute(text("UPDATE met.met_station SET active_flag = 0 WHERE basin_version_id = 'basin_v1'"))
        session.commit()
        monkeypatch.setattr(
            flood_alert_routes,
            "read_cached_tile_response",
            lambda *_args, **_kwargs: pytest.fail("cache lookup should not run for active-empty station source"),
        )
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_station_mvt_tile_bytes",
            lambda *_args, **_kwargs: pytest.fail("live SQL should not run for active-empty station source"),
        )
        monkeypatch.setattr(
            flood_alert_routes,
            "postgis_tile_sql",
            lambda *_args, **_kwargs: pytest.fail("tile SQL builder should not run for active-empty station source"),
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "MVT_SOURCE_IDENTITY_NOT_FOUND"
    assert body["error"]["details"] == {"layer_id": "met-stations", "basin_version_id": "basin_v1"}


def test_station_mvt_source_inventory_over_limit_fails_before_cache_or_live_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        limit = 1
        active_count = session.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM met.met_station
                WHERE basin_version_id = 'basin_v1'
                  AND active_flag = 1
                """
            )
        ).scalar_one()
        assert active_count == limit + 1

        monkeypatch.setattr(flood_alert_routes, "FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT", limit)
        monkeypatch.setattr(
            flood_alert_routes,
            "read_cached_tile_response",
            lambda *_args, **_kwargs: pytest.fail("cache lookup should not run for over-limit station source"),
        )
        monkeypatch.setattr(
            flood_alert_routes,
            "_fetch_station_mvt_tile_bytes",
            lambda *_args, **_kwargs: pytest.fail("live SQL should not run for over-limit station source"),
        )
        monkeypatch.setattr(
            flood_alert_routes,
            "postgis_tile_sql",
            lambda *_args, **_kwargs: pytest.fail("tile SQL builder should not run for over-limit station source"),
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "MVT_TILE_BUDGET_EXCEEDED"
    assert body["error"]["details"]["layer_id"] == "met-stations"
    assert body["error"]["details"]["basin_version_id"] == "basin_v1"
    assert body["error"]["details"]["limit_type"] == "source_inventory"
    assert body["error"]["details"]["feature_count"] == active_count
    assert body["error"]["details"]["max_features"] == limit


@pytest.mark.parametrize(
    ("tile_row", "expected_status", "expected_code", "expected_details"),
    [
        (
            {
                "tile": None,
                "source_identity_count": 1,
                "feature_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1,
                "coordinate_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES + 2,
                "feature_coordinate_overflow_count": 0,
                "feature_coordinate_count": 0,
                "coordinate_dimension_overflow_count": 0,
                "coordinate_dimension_count": 0,
                "invalid_property_count": 0,
                "invalid_properties": "",
            },
            413,
            "MVT_TILE_BUDGET_EXCEEDED",
            {
                "feature_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1,
                "coordinate_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES + 2,
            },
        ),
        (
            {
                "tile": b"must-not-return",
                "source_identity_count": 1,
                "feature_count": 1,
                "coordinate_count": 10,
                "feature_coordinate_overflow_count": 1,
                "feature_coordinate_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES + 1,
                "coordinate_dimension_overflow_count": 0,
                "coordinate_dimension_count": 0,
                "invalid_property_count": 0,
                "invalid_properties": "",
            },
            413,
            "MVT_TILE_BUDGET_EXCEEDED",
            {
                "limit_type": "feature_coordinates",
                "coordinate_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES + 1,
            },
        ),
        (
            {
                "tile": b"must-not-return",
                "source_identity_count": 1,
                "feature_count": 1,
                "coordinate_count": 1,
                "feature_coordinate_overflow_count": 0,
                "feature_coordinate_count": 0,
                "coordinate_dimension_overflow_count": 0,
                "coordinate_dimension_count": 0,
                "invalid_property_count": 2,
                "invalid_properties": "station_id,active_flag",
            },
            500,
            "MVT_PROPERTY_INVALID",
            {"invalid_property_count": 2, "properties": ["station_id", "active_flag"]},
        ),
    ],
)
def test_station_mvt_budget_and_required_property_errors_include_station_layer_details(
    monkeypatch: pytest.MonkeyPatch,
    tile_row: dict[str, Any],
    expected_status: int,
    expected_code: str,
    expected_details: dict[str, Any],
) -> None:
    fake_session = _StationMvtFakePostgresSession(tile_row=tile_row)
    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: fake_session
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/tiles/met-stations/basin_v1/6/12/24.pbf")
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-type"].split(";")[0] != flood_alert_routes.MVT_MEDIA_TYPE
    body = response.json()
    assert body["error"]["code"] == expected_code
    assert body["error"]["details"]["layer_id"] == "met-stations"
    for key, value in expected_details.items():
        assert body["error"]["details"][key] == value


def test_live_mvt_over_budget_stats_return_413_without_pbf(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self.row = row

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert parameters["collection_coordinate_limit"] == (
                    flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES
                )
                return FakeRowResult(
                    {
                        "tile": None,
                        "source_feature_count": 1,
                        "feature_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1,
                        "coordinate_count": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES + 2,
                    }
                )
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS budget test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(flood_alert_routes, "_require_flood_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{_iso(VALID_TIME_1)}/6/12/24.pbf"
            )
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-type"].split(";")[0] != flood_alert_routes.MVT_MEDIA_TYPE
    body = response.json()
    assert body["error"]["code"] == "MVT_TILE_BUDGET_EXCEEDED"
    assert body["error"]["details"]["feature_count"] == flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1
    assert body["error"]["details"]["coordinate_count"] == (
        flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_COLLECTION_MAX_COORDINATES + 2
    )


def test_live_mvt_feature_coordinate_overflow_returns_413_without_pbf(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self.row = row

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert "feature_coordinate_overflow_count" in sql
                assert parameters["feature_coordinate_limit"] == (
                    flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES
                )
                return FakeRowResult(
                    {
                        "tile": b"must-not-return",
                        "source_feature_count": 1,
                        "feature_count": 1,
                        "coordinate_count": 10,
                        "feature_coordinate_overflow_count": 1,
                        "feature_coordinate_count": (
                            flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES + 1
                        ),
                    }
                )
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS budget test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(flood_alert_routes, "_require_flood_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{_iso(VALID_TIME_1)}/6/12/24.pbf"
            )
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-type"].split(";")[0] != flood_alert_routes.MVT_MEDIA_TYPE
    body = response.json()
    assert body["error"]["code"] == "MVT_TILE_BUDGET_EXCEEDED"
    assert body["error"]["details"]["limit_type"] == "feature_coordinates"
    assert body["error"]["details"]["coordinate_count"] == (
        flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_FEATURE_MAX_COORDINATES + 1
    )


def test_live_mvt_invalid_required_properties_return_json_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self.row = row

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert "invalid_property_count" in sql
                assert "value::double precision IN" in sql
                assert parameters["variable"] == "q_down"
                return FakeRowResult(
                    {
                        "tile": b"must-not-return",
                        "source_feature_count": 1,
                        "feature_count": 1,
                        "coordinate_count": 10,
                        "invalid_property_count": 2,
                        "invalid_properties": "value,quality_flag",
                    }
                )
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS property test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(flood_alert_routes, "_require_hydro_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{_iso(VALID_TIME_1)}/6/12/24.pbf"
            )
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-type"].split(";")[0] != flood_alert_routes.MVT_MEDIA_TYPE
    body = response.json()
    assert body["error"]["code"] == "MVT_PROPERTY_INVALID"
    assert body["error"]["details"]["invalid_property_count"] == 2
    assert body["error"]["details"]["properties"] == ["value", "quality_flag"]


def test_hydro_and_river_network_mvt_routes_return_unavailable_without_live_postgis() -> None:
    with _client() as client:
        hydro = client.get(f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{_iso(VALID_TIME_1)}/4/12/6.pbf")
        non_ready_hydro = client.get(
            f"/api/v1/tiles/hydro/{RECOMPUTE_MOVED_PEAK_RUN_ID}/q_down/{_iso(VALID_TIME_1)}/4/12/6.pbf"
        )
        river = client.get("/api/v1/tiles/river-network/basin_v1/4/12/6.pbf")

    assert hydro.status_code == 424
    assert hydro.json()["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert non_ready_hydro.status_code == 409
    assert non_ready_hydro.json()["error"]["code"] == "FREQUENCY_NOT_COMPUTED"
    assert river.status_code == 424
    assert river.json()["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"


@pytest.mark.parametrize(
    ("path", "expected_layer", "expected_params"),
    [
        (
            "/api/v1/tiles/hydro/"
            f"{RECOMPUTE_MOVED_PEAK_RUN_ID}/q_down/{VALID_TIME_1.isoformat()}/4/12/6.pbf",
            "hydro",
            {
                "run_id": RECOMPUTE_MOVED_PEAK_RUN_ID,
                "variable": "q_down",
                "valid_time": VALID_TIME_1,
                "basin_version_id": "basin_v1",
                "river_network_version_id": "rnv_v1",
                "z": 4,
                "x": 12,
                "y": 6,
            },
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/4/12/6.pbf",
            "river-network",
            {"basin_version_id": "basin_v1", "z": 4, "x": 12, "y": 6},
        ),
    ],
)
def test_hydro_and_river_network_live_postgis_bind_requested_xyz(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    expected_layer: str,
    expected_params: dict[str, Any],
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeRowResult:
        def __init__(self, row: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None) -> None:
            self.row = row
            self.rows = rows if rows is not None else ([row] if row is not None else [])

        def mappings(self) -> FakeRowResult:
            return self

        def first(self) -> dict[str, Any] | None:
            return self.row

        def all(self) -> list[dict[str, Any]]:
            return self.rows

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRowResult:
            sql = str(statement)
            if "ST_TileEnvelope(:z, :x, :y)" in sql:
                assert f"'{expected_layer.replace('-', '_')}'" in sql or expected_layer == "hydro"
                for key, value in expected_params.items():
                    assert parameters[key] == value
                return FakeRowResult({"tile": b"live-tile", "source_identity_count": 1, "source_feature_count": 1})
            if "SELECT DISTINCT river_network_version_id" in sql:
                assert parameters["basin_version_id"] == "basin_v1"
                return FakeRowResult(None, [{"river_network_version_id": "rnv_v1"}])
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS tile test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(flood_alert_routes, "_require_hydro_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_run",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(path)
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.content == b"live-tile"


def test_mvt_invalid_xyz_fails_before_expensive_builders(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fail_if_called(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(flood_alert_routes, "_fetch_flood_mvt_tile_bytes", fail_if_called)

    with _client() as client:
        response = client.get(f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{_iso(VALID_TIME_1)}/3/8/0.pbf")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TILE_XYZ_INVALID"
    assert called is False


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/0/1/0.pbf",
        f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/0/0/1.pbf",
    ],
)
def test_mvt_invalid_low_zoom_xy_fails_with_tile_xyz_invalid(path: str) -> None:
    with _client() as client:
        response = client.get(path)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TILE_XYZ_INVALID"
    assert response.json()["error"]["details"]["max_exclusive"] == 1


@pytest.mark.parametrize(
    "path",
    [
        f"/api/v1/tiles/hydro/{RUN_ID}/velocity/{VALID_TIME_1_ISO}/6/12/24.pbf",
        f"/api/v1/tiles/flood-return-period/{RUN_ID}/2h/{VALID_TIME_1_ISO}/6/12/24.pbf",
        # Retired hydro variant must be rejected at the tile-route boundary
        # (mirrors the layer/valid-times deny). Split-string sentinel defeats
        # naive grep-replace from reintroducing the legacy variable id.
        f"/api/v1/tiles/hydro/{RUN_ID}/" + "wat" + "er_level" + f"/{VALID_TIME_1_ISO}/6/12/24.pbf",
        "/api/v1/tiles/hydro-national/" + "wat" + "er_level" + f"/{VALID_TIME_1_ISO}/6/12/24.pbf",
    ],
)
def test_unsupported_mvt_route_variants_fail_before_live_cache_or_sql(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, *_args: Any, **_kwargs: Any) -> object:
            raise AssertionError("unsupported route variants must fail before SQL/cache access")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_run",
        lambda *_args, **_kwargs: pytest.fail("_require_run called"),
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda *_args, **_kwargs: pytest.fail("_require_frequency_ready called"),
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "_fetch_hydro_mvt_tile_bytes",
        lambda *_args, **_kwargs: pytest.fail("hydro fetch called"),
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "_fetch_flood_mvt_tile_bytes",
        lambda *_args, **_kwargs: pytest.fail("flood fetch called"),
    )
    app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: FakeSession()
    try:
        with TestClient(app) as client:
            response = client.get(path)
    finally:
        app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_hydro_mvt_rejects_non_ready_run_before_cache_lookup_or_live_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_cache_lookup(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("hydro MVT cache lookup should not run for non-ready runs")

    def fail_live_fetch(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("hydro MVT live SQL should not run for non-ready runs")

    monkeypatch.setattr(flood_alert_routes, "read_cached_tile_response", fail_cache_lookup)
    monkeypatch.setattr(flood_alert_routes, "_fetch_hydro_mvt_tile_bytes", fail_live_fetch)

    with _client() as client:
        response = client.get(f"/api/v1/tiles/hydro/run_pending/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf")

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "FREQUENCY_NOT_COMPUTED"
    assert body["error"]["details"]["status"] == "parsed"


def test_hydro_mvt_rejects_non_ready_run_even_when_matching_cache_row_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        seed_tile = flood_alert_routes.TileInput(
            layer_id="discharge",
            source_id="run_pending",
            source_version=flood_alert_routes._run_source_version(
                flood_alert_routes._require_run(session, "run_pending")
            ),
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="variable:q_down",
        )
        flood_alert_routes.build_raw_tile_response(session, seed_tile, b"stale-non-ready-cache")

        def fail_cache_lookup(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("hydro MVT cache lookup should not run for non-ready runs")

        def fail_live_fetch(*_args: Any, **_kwargs: Any) -> bytes:
            raise AssertionError("hydro MVT live SQL should not run for non-ready runs")

        monkeypatch.setattr(flood_alert_routes, "read_cached_tile_response", fail_cache_lookup)
        monkeypatch.setattr(flood_alert_routes, "_fetch_hydro_mvt_tile_bytes", fail_live_fetch)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(f"/api/v1/tiles/hydro/run_pending/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FREQUENCY_NOT_COMPUTED"


@pytest.mark.parametrize(
    ("path", "fetch_name", "unexpected_sql"),
    [
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_2_ISO}/6/12/24.pbf",
            "_fetch_hydro_mvt_tile_bytes",
            "map.tile_cache",
        ),
        (
            "/api/v1/tiles/flood-return-period/"
            f"{RUN_ID}/1h/{(VALID_TIME_1 + timedelta(days=30)).isoformat().replace('+00:00', 'Z')}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
            "map.tile_cache",
        ),
        (
            "/api/v1/tiles/river-network/basin_missing/6/12/24.pbf",
            "_fetch_river_network_mvt_tile_bytes",
            "map.tile_cache",
        ),
        (
            "/api/v1/tiles/met-stations/basin_missing/6/12/24.pbf",
            "_fetch_station_mvt_tile_bytes",
            "map.tile_cache",
        ),
    ],
)
def test_absent_mvt_source_identity_fails_before_cache_lookup_or_live_sql(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    fetch_name: str,
    unexpected_sql: str,
) -> None:
    def fail_cache_lookup(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("cache lookup should not run for absent MVT source identity")

    def fail_live_fetch(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("live PostGIS fetch should not run for absent MVT source identity")

    monkeypatch.setattr(flood_alert_routes, "read_cached_tile_response", fail_cache_lookup)
    monkeypatch.setattr(flood_alert_routes, fetch_name, fail_live_fetch)
    monkeypatch.setattr(
        flood_alert_routes,
        "postgis_tile_sql",
        lambda *_args, **_kwargs: pytest.fail("full tile SQL builder should not run for absent source identity"),
    )

    with _client() as client:
        response = client.get(path)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "MVT_SOURCE_IDENTITY_NOT_FOUND"
    assert unexpected_sql not in str(body)


def test_mvt_source_identity_preflight_sql_uses_index_friendly_public_route_keys() -> None:
    class CapturingResult:
        def mappings(self) -> CapturingResult:
            return self

        def all(self) -> list[dict[str, Any]]:
            return []

        def first(self) -> None:
            return None

    class CapturingSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def execute(self, statement: Any, parameters: dict[str, Any]) -> CapturingResult:
            self.calls.append((re.sub(r"\s+", " ", str(statement)).strip(), parameters))
            return CapturingResult()

    hydro_session = CapturingSession()
    with pytest.raises(flood_alert_routes.ApiError):
        flood_alert_routes._require_hydro_mvt_source_identity(
            hydro_session,
            run_id=RUN_ID,
            variable="q_down",
            valid_time=VALID_TIME_1,
            basin_version_id="basin_v1",
            river_network_version_id="rnv_v1",
        )

    flood_session = CapturingSession()
    with pytest.raises(flood_alert_routes.ApiError):
        flood_alert_routes._require_flood_mvt_source_identity(
            flood_session,
            run_id=RUN_ID,
            duration="1h",
            valid_time=VALID_TIME_1,
            basin_version_id="basin_v1",
            river_network_version_id="rnv_v1",
        )

    river_session = CapturingSession()
    with pytest.raises(flood_alert_routes.ApiError):
        flood_alert_routes._river_network_source_version(river_session, "basin_missing")

    hydro_sql, hydro_params = hydro_session.calls[0]
    flood_sql, flood_params = flood_session.calls[0]
    river_sql, river_params = river_session.calls[0]
    assert "FROM hydro.river_timeseries WHERE run_id = :run_id" in hydro_sql
    assert "AND basin_version_id = :basin_version_id" in hydro_sql
    assert "AND river_network_version_id = :river_network_version_id" in hydro_sql
    assert "AND variable = :variable AND valid_time = :valid_time" in hydro_sql
    assert "LIMIT 1" in hydro_sql
    assert hydro_params == {
        "run_id": RUN_ID,
        "variable": "q_down",
        "valid_time": VALID_TIME_1,
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rnv_v1",
    }
    assert "FROM flood.return_period_result WHERE run_id = :run_id" in flood_sql
    assert "AND basin_version_id = :basin_version_id" in flood_sql
    assert "AND river_network_version_id = :river_network_version_id" in flood_sql
    assert "AND duration = :duration AND max_over_window = false AND valid_time = :valid_time" in flood_sql
    assert "LIMIT 1" in flood_sql
    assert flood_params == {
        "run_id": RUN_ID,
        "duration": "1h",
        "valid_time": VALID_TIME_1,
        "basin_version_id": "basin_v1",
        "river_network_version_id": "rnv_v1",
    }
    assert "FROM core.river_network_version WHERE basin_version_id = :basin_version_id" in river_sql
    assert "ORDER BY river_network_version_id" in river_sql
    assert "core.model_instance" not in river_sql
    assert river_params == {"basin_version_id": "basin_missing"}


def test_station_mvt_source_version_uses_active_bounded_structured_inventory() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class CapturingRows:
        def mappings(self) -> CapturingRows:
            return self

        def all(self) -> list[dict[str, Any]]:
            return [
                {
                    "station_id": "station|001",
                    "basin_version_id": "basin_v1",
                    "station_name": "Name|With|Pipes",
                    "station_role": "forcing_proxy",
                    "active_flag": True,
                    "geom": "EWKB|hex|text",
                    "created_at": datetime(2026, 5, 3, tzinfo=UTC),
                }
            ]

    class CapturingSession:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> CapturingRows:
            self.calls.append((str(statement), parameters))
            return CapturingRows()

    session = CapturingSession()
    source_version = flood_alert_routes._station_source_version(session, "basin_v1")
    sql, params = session.calls[0]
    normalized_sql = re.sub(r"\s+", " ", sql)

    assert source_version.startswith("met-stations:")
    assert source_version.endswith(":basin_v1:1:station|001:station|001")
    assert "WHERE basin_version_id = :basin_version_id AND active_flag = true" in normalized_sql
    assert "ORDER BY station_id LIMIT :limit" in normalized_sql
    assert "encode(ST_AsEWKB(geom), 'hex')" in normalized_sql
    assert "concat_ws" not in sql
    assert "string_agg" not in sql
    assert "md5" not in sql
    assert params == {
        "basin_version_id": "basin_v1",
        "limit": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1,
    }


def test_station_mvt_source_version_rejects_unbounded_active_inventory_before_hashing() -> None:
    class FakeDialect:
        name = "postgresql"

    class FakeBind:
        dialect = FakeDialect()

    class OverLimitRows:
        def mappings(self) -> OverLimitRows:
            return self

        def all(self) -> list[dict[str, Any]]:
            return [
                {
                    "station_id": f"station_{index:05d}",
                    "basin_version_id": "basin_v1",
                    "station_name": f"Station {index}",
                    "station_role": "forcing_proxy",
                    "active_flag": True,
                    "geom": f"geom-{index}",
                    "created_at": VALID_TIME_1,
                }
                for index in range(flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1)
            ]

    class FakeSession:
        def get_bind(self) -> FakeBind:
            return FakeBind()

        def execute(self, statement: Any, parameters: dict[str, Any]) -> OverLimitRows:
            sql = str(statement)
            assert "LIMIT :limit" in sql
            assert parameters["limit"] == flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1
            return OverLimitRows()

    with pytest.raises(flood_alert_routes.ApiError) as exc_info:
        flood_alert_routes._station_source_version(FakeSession(), "basin_v1")

    exc = exc_info.value
    assert exc.status_code == 413
    assert exc.code == "MVT_TILE_BUDGET_EXCEEDED"
    assert exc.details["layer_id"] == "met-stations"
    assert exc.details["limit_type"] == "source_inventory"
    assert exc.details["feature_count"] == flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1
    assert exc.details["max_features"] == flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT


@pytest.mark.parametrize(
    ("path", "fetch_name", "table_name", "seed_sql"),
    [
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_hydro_mvt_tile_bytes",
            "hydro.river_timeseries",
            """
            DELETE FROM hydro.river_timeseries
            WHERE run_id = :run_id
              AND variable = 'q_down'
              AND valid_time = :valid_time
            """,
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
            "flood.return_period_result",
            """
            DELETE FROM flood.return_period_result
            WHERE run_id = :run_id
              AND duration = '1h'
              AND max_over_window = false
              AND valid_time = :valid_time
            """,
        ),
    ],
)
def test_run_scoped_mvt_rejects_sibling_only_source_identity_before_cache_or_live_sql(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    fetch_name: str,
    table_name: str,
    seed_sql: str,
) -> None:
    with _store() as session:
        seed_tile = (
            flood_alert_routes.TileInput(
                layer_id="discharge",
                source_id=RUN_ID,
                source_version="sibling-source-version",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:q_down",
            )
            if table_name == "hydro.river_timeseries"
            else flood_alert_routes.TileInput(
                layer_id="flood-return-period",
                source_id=RUN_ID,
                source_version="sibling-source-version",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="duration:1h",
            )
        )
        flood_alert_routes.build_raw_tile_response(session, seed_tile, b"sibling-cache")
        session.execute(text(seed_sql), {"run_id": RUN_ID, "valid_time": VALID_TIME_1})
        if table_name == "hydro.river_timeseries":
            session.execute(
                text(
                    """
                    INSERT INTO hydro.river_timeseries (
                        run_id, basin_version_id, river_network_version_id, river_segment_id,
                        valid_time, variable, value, unit
                    )
                    VALUES (:run_id, 'basin_v2', 'rnv_v2', 'seg_004', :valid_time, 'q_down', 999.0, 'm3/s')
                    """
                ),
                {"run_id": RUN_ID, "valid_time": VALID_TIME_1},
            )
        else:
            _insert_result(
                session,
                "seg_004",
                "basin_v2",
                "rnv_v2",
                VALID_TIME_1,
                999.0,
                99.0,
                "severe",
                False,
            )
            _refresh_run_quality(session, RUN_ID)
        session.commit()
        monkeypatch.setattr(
            flood_alert_routes,
            "read_cached_tile_response",
            lambda *_args, **_kwargs: pytest.fail("cache read should not run for sibling-only source identity"),
        )
        monkeypatch.setattr(
            flood_alert_routes,
            fetch_name,
            lambda *_args, **_kwargs: pytest.fail("live SQL should not run for sibling-only source identity"),
        )
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "MVT_SOURCE_IDENTITY_NOT_FOUND"
    assert body["error"]["details"]["basin_version_id"] == "basin_v1"
    assert body["error"]["details"]["river_network_version_id"] == "rnv_v1"


@pytest.mark.parametrize(
    ("path", "fetch_name"),
    [
        (f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf", "_fetch_hydro_mvt_tile_bytes"),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/3h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/6h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/24h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/72h/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
        ),
        (
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/7d/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_flood_mvt_tile_bytes",
        ),
    ],
)
def test_advertised_mvt_route_variables_are_accepted_by_route_validation(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    fetch_name: str,
) -> None:
    monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
    monkeypatch.setattr(flood_alert_routes, "_require_hydro_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_mvt_source_identity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(flood_alert_routes, "_require_flood_product_ready", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(flood_alert_routes, "_require_flood_route_product_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_run",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_frequency_ready",
        lambda _session, _run_id: {"river_network_version_id": "rnv_v1", "basin_version_id": "basin_v1"},
    )
    monkeypatch.setattr(flood_alert_routes, fetch_name, lambda *_args, **_kwargs: b"accepted")

    with _client() as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.content == b"accepted"


def test_layer_metadata_discovery_exposes_mvt_contract() -> None:
    with _client() as client:
        response = client.get("/api/v1/layers")
        valid_times = client.get("/api/v1/layers/flood-return-period/valid-times")

    assert response.status_code == 200
    layers = response.json()["data"]
    flood_layer = next(layer for layer in layers if layer["layer_id"] == "flood-return-period")
    metadata = flood_layer["metadata"]
    assert metadata["layer_id"] == "flood-return-period"
    assert metadata["tile_format"] == "mvt"
    assert metadata["url_template"] == (
        "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert metadata["release_blocking"] is True
    assert metadata["maplibre_source_layer"] == "flood_return_period"
    assert metadata["property_schema_version"] == "m16-hydrology-mvt-v1"
    assert metadata["min_zoom"] == 0
    assert metadata["max_zoom"] == 14
    assert metadata["bounds_crs"] == "EPSG:3857"
    assert metadata["cache_etag"].startswith('W/"metadata-')
    assert metadata["fallback_available"] is True
    assert metadata["production_mvt_readiness_claimed"] is False
    assert metadata["valid_time_limit"] == MVT_VALID_TIME_SAMPLE_LIMIT
    assert metadata["valid_time_observed_count"] <= MVT_VALID_TIME_SAMPLE_LIMIT + 1
    assert metadata["valid_times_truncated"] is False
    assert valid_times.status_code == 200
    valid_time_data = valid_times.json()["data"]
    assert _iso(VALID_TIME_1) in valid_time_data["valid_times"]
    assert valid_time_data["items"] == valid_time_data["valid_times"]
    assert valid_time_data["limit"] == MVT_VALID_TIME_SAMPLE_LIMIT
    assert valid_time_data["observed_count"] <= MVT_VALID_TIME_SAMPLE_LIMIT + 1
    assert valid_time_data["truncated"] is False


def test_layer_valid_times_budget_caps_catalog_and_endpoint() -> None:
    run_id = "zz_valid_time_budget"
    first_valid_time = datetime(2026, 5, 20, tzinfo=UTC)
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": first_valid_time,
                "start_time": first_valid_time,
                "end_time": first_valid_time + timedelta(hours=MVT_VALID_TIME_SAMPLE_LIMIT + 5),
            },
        )
        for offset in range(MVT_VALID_TIME_SAMPLE_LIMIT + 5):
            valid_time = first_valid_time + timedelta(hours=offset)
            _insert_result(
                session,
                "seg_001",
                "basin_v1",
                "rnv_v1",
                valid_time,
                100.0 + offset,
                2.0,
                "normal",
                False,
                run_id=run_id,
            )
            _insert_timeseries_result(session, "seg_001", run_id, valid_time, 100.0 + offset)
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                layers_response = client.get("/api/v1/layers")
                flood_valid_times_response = client.get("/api/v1/layers/flood-return-period/valid-times")
                discharge_valid_times_response = client.get("/api/v1/layers/discharge/valid-times")
                unsupported_valid_times_response = client.get("/api/v1/layers/river-network/valid-times")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert layers_response.status_code == 200
    metadata_by_layer = {layer["layer_id"]: layer["metadata"] for layer in layers_response.json()["data"]}
    latest_window_start = first_valid_time + timedelta(hours=5)
    newest_valid_time = first_valid_time + timedelta(hours=MVT_VALID_TIME_SAMPLE_LIMIT + 4)
    for layer_id in ("flood-return-period", "warning-level", "discharge"):
        metadata = metadata_by_layer[layer_id]
        assert len(metadata["valid_times"]) == MVT_VALID_TIME_SAMPLE_LIMIT
        assert metadata["valid_time_limit"] == MVT_VALID_TIME_SAMPLE_LIMIT
        assert metadata["valid_time_observed_count"] == MVT_VALID_TIME_SAMPLE_LIMIT + 1
        assert metadata["valid_times_truncated"] is True
        assert metadata["valid_times"] == sorted(metadata["valid_times"])
        assert metadata["valid_times"][0] == _iso(latest_window_start)
        assert metadata["valid_times"][-1] == _iso(newest_valid_time)

    for response in (flood_valid_times_response, discharge_valid_times_response):
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data["valid_times"]) == MVT_VALID_TIME_SAMPLE_LIMIT
        assert data["items"] == data["valid_times"]
        assert data["limit"] == MVT_VALID_TIME_SAMPLE_LIMIT
        assert data["observed_count"] == MVT_VALID_TIME_SAMPLE_LIMIT + 1
        assert data["truncated"] is True
        assert data["valid_times"] == sorted(data["valid_times"])
        assert data["valid_times"][0] == _iso(latest_window_start)
        assert data["valid_times"][-1] == _iso(newest_valid_time)

    assert unsupported_valid_times_response.status_code == 200
    unsupported = unsupported_valid_times_response.json()["data"]
    assert unsupported == {
        "valid_times": [],
        "items": [],
        "limit": MVT_VALID_TIME_SAMPLE_LIMIT,
        "observed_count": 0,
        "truncated": False,
    }


def test_layer_valid_times_endpoint_scopes_to_explicit_run_id_latest_window() -> None:
    old_run_id = "aa_valid_time_old_run"
    new_run_id = "zz_valid_time_new_run"
    old_start = datetime(2026, 5, 20, tzinfo=UTC)
    new_start = datetime(2026, 5, 21, tzinfo=UTC)
    with _store() as session:
        for run_id, start in ((old_run_id, old_start), (new_run_id, new_start)):
            session.execute(
                text(
                    """
                    INSERT INTO hydro.hydro_run (
                        run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                        start_time, end_time, status, run_manifest_uri
                    )
                    VALUES (
                        :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                        'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "cycle_time": start,
                    "start_time": start,
                    "end_time": start + timedelta(hours=MVT_VALID_TIME_SAMPLE_LIMIT + 5),
                },
            )
            for offset in range(MVT_VALID_TIME_SAMPLE_LIMIT + 5):
                valid_time = start + timedelta(hours=offset)
                _insert_result(
                    session,
                    "seg_001",
                    "basin_v1",
                    "rnv_v1",
                    valid_time,
                    100.0 + offset,
                    2.0,
                    "normal",
                    False,
                    run_id=run_id,
                )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                scoped = client.get(f"/api/v1/layers/flood-return-period/valid-times?run_id={old_run_id}")
                unscoped = client.get("/api/v1/layers/flood-return-period/valid-times")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert scoped.status_code == 200
    scoped_data = scoped.json()["data"]
    assert scoped_data["valid_times"][0] == _iso(old_start + timedelta(hours=5))
    assert scoped_data["valid_times"][-1] == _iso(old_start + timedelta(hours=MVT_VALID_TIME_SAMPLE_LIMIT + 4))
    assert scoped_data["valid_times"] == sorted(scoped_data["valid_times"])

    assert unscoped.status_code == 200
    unscoped_data = unscoped.json()["data"]
    assert unscoped_data["valid_times"][-1] == _iso(new_start + timedelta(hours=MVT_VALID_TIME_SAMPLE_LIMIT + 4))
    assert unscoped_data["valid_times"][0] != scoped_data["valid_times"][0]


def _seed_second_basin_national_run(session: Session) -> tuple[datetime, datetime]:
    """Add a second river-network (rnv_v2) frequency_done run with its own q_down series.

    The default seed already gives rnv_v1 a frequency_done run (RUN_ID) with q_down at
    VALID_TIME_1. This adds a model_instance for rnv_v2 plus a frequency_done run with
    q_down at a distinct time so the national union spans two networks.
    """
    rnv2_run_id = "fcst_gfs_2026050300_rnv_v2"
    rnv2_valid_time = VALID_TIME_2 + timedelta(hours=3)
    # Make RUN_ID the latest frequency-ready run for rnv_v1 so the national selector
    # picks the run that actually carries q_down for that network.
    session.execute(
        text("UPDATE hydro.hydro_run SET cycle_time = :cycle_time WHERE run_id = :run_id"),
        {"cycle_time": datetime(2026, 5, 4, tzinfo=UTC), "run_id": RUN_ID},
    )
    session.execute(
        text(
            """
            INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id)
            VALUES ('model_2', 'basin_v2', 'rnv_v2')
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                start_time, end_time, status, run_manifest_uri, updated_at
            )
            VALUES (
                :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_2', 'basin_v2',
                'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest', :updated_at
            )
            """
        ),
        {
            "run_id": rnv2_run_id,
            "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
            "start_time": datetime(2026, 5, 3, tzinfo=UTC),
            "end_time": datetime(2026, 5, 10, tzinfo=UTC),
            "updated_at": datetime(2026, 5, 3, 1, tzinfo=UTC),
        },
    )
    session.execute(
        text(
            """
            INSERT INTO hydro.river_timeseries (
                run_id, basin_version_id, river_network_version_id, river_segment_id,
                valid_time, variable, value, unit
            )
            VALUES (
                :run_id, 'basin_v2', 'rnv_v2', 'seg_004',
                :valid_time, 'q_down', 305.0, 'm3/s'
            )
            """
        ),
        {"run_id": rnv2_run_id, "valid_time": rnv2_valid_time},
    )
    session.commit()
    return VALID_TIME_1, rnv2_valid_time


def test_unscoped_discharge_catalog_uses_national_template_and_union_valid_times() -> None:
    with _store() as session:
        rnv1_time, rnv2_time = _seed_second_basin_national_run(session)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/layers")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    discharge = next(layer for layer in response.json()["data"] if layer["layer_id"] == "discharge")
    metadata = discharge["metadata"]
    assert metadata["tile_url_template"] == "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    assert "{run_id}" not in metadata["tile_url_template"]
    assert metadata["url_template"] == metadata["tile_url_template"]
    assert metadata["required_placeholders"] == ["valid_time", "z", "x", "y"]
    assert metadata["maplibre_source_layer"] == "hydro"
    # National union tiles are gated to z>=3: low zoom no longer overruns the per-tile
    # budget because postgis_tile_sql("hydro-national") generalizes to the q_down trunk
    # (per-network PERCENT_RANK) and coarsens geometry by zoom. min_zoom=3 aligns with the
    # front-end initial national view (zoom 3.35) so trunk rivers show without zooming in.
    # Guards against regressing the hardcoded min_zoom=0 in layer_metadata().
    assert metadata["min_zoom"] == 3
    valid_times = metadata["valid_times"]
    assert _iso(rnv1_time) in valid_times
    assert _iso(rnv2_time) in valid_times
    assert valid_times == sorted(valid_times)


def test_unscoped_discharge_valid_times_endpoint_returns_union() -> None:
    with _store() as session:
        rnv1_time, rnv2_time = _seed_second_basin_national_run(session)
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/layers/discharge/valid-times")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    data = response.json()["data"]
    assert _iso(rnv1_time) in data["valid_times"]
    assert _iso(rnv2_time) in data["valid_times"]
    assert data["items"] == data["valid_times"]
    assert data["valid_times"] == sorted(data["valid_times"])


def test_scoped_discharge_catalog_returns_national_template() -> None:
    """Spec invariant (overview-data-contracts: *Default discharge tile URL is national across all
    `/api/v1/layers` callers*): even when the HTTP caller passes `?run_id=<X>` (simulating the
    frontend enrichment `fetchLayers(latestRun.run_id)` call), the discharge entry's tile URL
    template MUST be the national one (no `{run_id}` placeholder). Issue #601 root cause: this
    test originally pinned the bug behavior (single-run template under `?run_id=<X>`); flipped
    per the spec so the contract is the regression sentinel for the bug, not for the bug fix.
    """
    with _client() as client:
        response = client.get(f"/api/v1/layers?run_id={RUN_ID}")

    assert response.status_code == 200
    discharge = next(layer for layer in response.json()["data"] if layer["layer_id"] == "discharge")
    metadata = discharge["metadata"]
    assert metadata["tile_url_template"] == "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    assert metadata["required_placeholders"] == ["valid_time", "z", "x", "y"]
    assert "{run_id}" not in metadata["tile_url_template"]
    assert "run_id" not in metadata["required_placeholders"]


def test_hydro_national_tile_sql_binds_only_variable_and_valid_time() -> None:
    statement = flood_alert_routes.postgis_tile_sql("hydro-national")
    sql = re.sub(r"\s+", " ", statement)
    source_cte = sql[sql.index("source_rows AS") : sql.index("bounded_rows AS")]
    assert ":variable" in source_cte
    assert ":valid_time" in source_cte
    assert ":run_id" not in source_cte
    assert ":basin_version_id" not in source_cte
    assert ":river_network_version_id" not in source_cte
    assert "DISTINCT ON (mi.river_network_version_id)" in source_cte
    assert "ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC" in source_cte
    # National reuses the "hydro" maplibre source layer name for frontend parity.
    assert "ST_AsMVT(tile_rows, 'hydro'," in sql


def test_hydro_national_tile_sql_generalizes_trunk_by_zoom() -> None:
    statement = flood_alert_routes.postgis_tile_sql("hydro-national")
    sql = re.sub(r"\s+", " ", statement)
    source_cte = sql[sql.index("source_rows AS") : sql.index("bounded_rows AS")]
    # Trunk proxy: per-network PERCENT_RANK over q_down (value); :z drives the cutoff.
    assert "PERCENT_RANK() OVER ( PARTITION BY ts.river_network_version_id" in source_cte
    assert "value_percent_rank" in source_cte
    # Zoom-keyed value-threshold filter lives in the source CTE (before budget counting).
    # Progressive trunk cutoff extends through z7/z8 so dense basins (Heihe) stay inside
    # the per-tile budget; full detail only at z>=9.
    assert ":z >= 9" in source_cte
    assert ":z <= 4 THEN 0.90" in source_cte
    assert ":z = 5 THEN 0.70" in source_cte
    assert ":z = 6 THEN 0.40" in source_cte
    assert ":z = 7 THEN 0.15" in source_cte
    # NULL-value segments are dropped at low zoom (only the z>=9 branch keeps them).
    assert "value_percent_rank IS NOT NULL" in source_cte
    # Per-zoom coarse simplification on the source geom, topology preserved.
    assert "ST_SimplifyPreserveTopology" in source_cte
    assert ":z <= 4 THEN 2000.0" in source_cte
    assert ":z = 7 THEN 200.0" in source_cte
    # National still binds only :variable/:valid_time for identity (run/network resolved
    # by DISTINCT ON); :z is the shared tile-envelope bind, not a national identity param.
    assert ":run_id" not in source_cte
    assert ":basin_version_id" not in source_cte
    assert ":river_network_version_id" not in source_cte


def test_single_run_hydro_tile_sql_has_no_zoom_trunk_filter() -> None:
    # Zero-regression guard: the single-run "hydro" layer must not gain the national
    # trunk-generalization clauses.
    sql = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("hydro"))
    source_cte = sql[sql.index("source_rows AS") : sql.index("bounded_rows AS")]
    assert "PERCENT_RANK" not in source_cte
    assert "value_percent_rank" not in source_cte
    assert ":z <= 4 THEN 0.90" not in source_cte


def test_hydro_national_tile_sql_self_describes_basin_id() -> None:
    # National click→popup must resolve a basin without an N+1 versions fetch: the tile
    # LEFT JOINs core.basin_version and emits basin_id as a public MVT property.
    sql = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("hydro-national"))
    source_cte = sql[sql.index("source_rows AS") : sql.index("bounded_rows AS")]
    assert "LEFT JOIN core.basin_version bv ON bv.basin_version_id = ts.basin_version_id" in source_cte
    assert "bv.basin_id" in source_cte
    # basin_id rides through the public projection (ST_AsMVT tile_rows).
    tile_rows = sql[sql.index("SELECT ST_AsMVT(tile_rows") :]
    assert "basin_id" in tile_rows


def test_single_run_hydro_tile_sql_has_no_basin_id_column() -> None:
    # Zero-regression: single-run "hydro" tile does not self-describe basin_id (basin is
    # known from the run context), so it must not gain the national-only column.
    sql = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("hydro"))
    assert "bv.basin_id" not in sql
    assert "core.basin_version" not in sql


@pytest.mark.parametrize(
    "layer_id",
    ["flood-return-period", "warning-level", "discharge"],
)
def test_layer_valid_times_explicit_non_ready_run_requires_frequency_ready_before_discovery(
    monkeypatch: pytest.MonkeyPatch,
    layer_id: str,
) -> None:
    monkeypatch.setattr(
        flood_alert_routes,
        "valid_times_for_layer",
        lambda *_args, **_kwargs: pytest.fail("valid_times_for_layer called for non-ready run"),
    )

    with _client() as client:
        response = client.get(f"/api/v1/layers/{layer_id}/valid-times?run_id=run_pending")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "FREQUENCY_NOT_COMPUTED"


@pytest.mark.parametrize(
    "layer_id",
    ["flood-return-period", "warning-level", "discharge"],
)
def test_layer_valid_times_unscoped_no_ready_run_returns_empty_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
    layer_id: str,
) -> None:
    monkeypatch.setattr(
        flood_alert_routes,
        "valid_times_for_layer",
        lambda *_args, **_kwargs: pytest.fail("valid_times_for_layer called without a ready run"),
    )
    with _store() as session:
        session.execute(text("UPDATE hydro.hydro_run SET status = 'parsed'"))
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(f"/api/v1/layers/{layer_id}/valid-times")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "valid_times": [],
        "items": [],
        "limit": MVT_VALID_TIME_SAMPLE_LIMIT,
        "observed_count": 0,
        "truncated": False,
    }


def test_layer_catalog_unscoped_no_ready_run_returns_empty_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        flood_alert_routes,
        "valid_times_for_layer",
        lambda *_args, **_kwargs: pytest.fail("valid_times_for_layer called without a ready run"),
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "read_cached_tile_response",
        lambda *_args, **_kwargs: pytest.fail("tile cache lookup called during layer discovery"),
    )
    monkeypatch.setattr(
        flood_alert_routes,
        "postgis_tile_sql",
        lambda *_args, **_kwargs: pytest.fail("live tile SQL called during layer discovery"),
    )
    with _store() as session:
        session.execute(text("UPDATE hydro.hydro_run SET status = 'parsed'"))
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/layers")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_layer_catalog_unscoped_frequency_ready_without_flood_product_still_exposes_discharge() -> None:
    """无洪频基线的 frequency-ready run（QHH/Heihe 现实：有 q_down 无 return-period）仍应暴露 discharge。

    解耦回归：目录默认 run 选最新 frequency-ready（latest_frequency_ready_run），不再像 latest_ready_run
    那样内连接 flood.return_period_result。discharge/river-network 暴露，flood/warning 层
    仍在目录但被 _annotate_flood_layer_quality 标注 unavailable，不阻塞水文图层。
    """
    with _store() as session:
        # RUN_ID 设为唯一 frequency-ready run，并移除其洪频产品
        rid = {"rid": RUN_ID}
        session.execute(text("UPDATE hydro.hydro_run SET status = 'parsed' WHERE run_id != :rid"), rid)
        session.execute(text("UPDATE hydro.hydro_run SET status = 'frequency_done' WHERE run_id = :rid"), rid)
        session.execute(text("DELETE FROM flood.return_period_result WHERE run_id = :rid"), rid)
        _refresh_run_quality(session, RUN_ID)
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/layers")
                discharge_valid_times = client.get("/api/v1/layers/discharge/valid-times")
                flood_valid_times = client.get("/api/v1/layers/flood-return-period/valid-times")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 200
    data = response.json()["data"]
    by_id = {layer["layer_id"]: layer for layer in data}
    # 无洪频仍暴露 discharge，且带可渲染 MVT 瓦片契约
    assert "discharge" in by_id
    assert by_id["discharge"]["metadata"]["tile_url_template"]
    assert "river-network" in by_id
    # 洪频/预警层仍在目录但被标注 unavailable（return_period_result 缺）
    flood = by_id["flood-return-period"]
    assert "return_period_result" in flood["metadata"]["unavailable_products"]
    # valid-times 端点同样解耦：discharge 有有效时间（地图叠加层可渲染），洪频则空（不抛错）
    assert discharge_valid_times.status_code == 200
    assert len(discharge_valid_times.json()["data"]["valid_times"]) > 0
    assert flood_valid_times.status_code == 200
    assert flood_valid_times.json()["data"]["valid_times"] == []


def test_layer_catalog_explicit_missing_run_source_identity_returns_stable_error_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "ready_missing_identity_catalog"
    monkeypatch.setattr(
        flood_alert_routes,
        "valid_times_for_layer",
        lambda *_args, **_kwargs: pytest.fail("valid_times_for_layer called before source identity preflight"),
    )
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_without_identity', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": VALID_TIME_1,
                "start_time": VALID_TIME_1,
                "end_time": VALID_TIME_2,
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            VALID_TIME_1,
            100.0,
            2.0,
            "normal",
            True,
            run_id=run_id,
        )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(f"/api/v1/layers?run_id={run_id}")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "MVT_SOURCE_IDENTITY_NOT_FOUND"
    assert body["error"]["details"]["run_id"] == run_id
    assert body["error"]["details"]["basin_version_id"] == "basin_v1"
    assert body["error"]["details"]["river_network_version_id"] is None


@pytest.mark.parametrize("layer_id", ["flood-return-period", "warning-level", "discharge"])
def test_layer_valid_times_explicit_missing_run_source_identity_returns_stable_error_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
    layer_id: str,
) -> None:
    run_id = f"ready_missing_identity_{layer_id.replace('-', '_')}"
    monkeypatch.setattr(
        flood_alert_routes,
        "valid_times_for_layer",
        lambda *_args, **_kwargs: pytest.fail("valid_times_for_layer called before source identity preflight"),
    )
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_without_identity', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": VALID_TIME_1,
                "start_time": VALID_TIME_1,
                "end_time": VALID_TIME_2,
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            VALID_TIME_1,
            100.0,
            2.0,
            "normal",
            True,
            run_id=run_id,
        )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(f"/api/v1/layers/{layer_id}/valid-times?run_id={run_id}")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "MVT_SOURCE_IDENTITY_NOT_FOUND"
    assert body["error"]["details"]["layer_id"] == layer_id
    assert body["error"]["details"]["run_id"] == run_id
    assert body["error"]["details"]["river_network_version_id"] is None


def test_layer_catalog_unscoped_latest_ready_missing_source_identity_returns_stable_error_without_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "zz_ready_missing_identity_latest"
    monkeypatch.setattr(
        flood_alert_routes,
        "valid_times_for_layer",
        lambda *_args, **_kwargs: pytest.fail("valid_times_for_layer called before source identity preflight"),
    )
    with _store() as session:
        session.execute(text("UPDATE hydro.hydro_run SET status = 'parsed'"))
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_without_identity', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": VALID_TIME_2,
                "start_time": VALID_TIME_1,
                "end_time": VALID_TIME_2,
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            VALID_TIME_1,
            100.0,
            2.0,
            "normal",
            True,
            run_id=run_id,
        )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                catalog_response = client.get("/api/v1/layers")
                valid_times_response = client.get("/api/v1/layers/flood-return-period/valid-times")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    for response in (catalog_response, valid_times_response):
        assert response.status_code == 404
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "MVT_SOURCE_IDENTITY_NOT_FOUND"
        assert body["error"]["details"]["run_id"] == run_id
        assert body["error"]["details"]["river_network_version_id"] is None


@pytest.mark.parametrize(
    ("layer_id", "expected_table", "expected_identity"),
    [
        (
            "flood-return-period",
            "flood.return_period_result",
            (
                "run_id = :run_id",
                "basin_version_id = :basin_version_id",
                "river_network_version_id = :river_network_version_id",
                "duration = :duration",
            ),
        ),
        (
            "warning-level",
            "flood.return_period_result",
            (
                "run_id = :run_id",
                "basin_version_id = :basin_version_id",
                "river_network_version_id = :river_network_version_id",
                "duration = :duration",
            ),
        ),
        (
            "discharge",
            "hydro.river_timeseries",
            (
                "run_id = :run_id",
                "basin_version_id = :basin_version_id",
                "river_network_version_id = :river_network_version_id",
                "variable = :variable",
            ),
        ),
    ],
)
def test_valid_times_for_layer_concrete_run_uses_direct_index_friendly_predicate(
    layer_id: str,
    expected_table: str,
    expected_identity: tuple[str, str],
) -> None:
    class FakeRows:
        def mappings(self) -> "FakeRows":
            return self

        def all(self) -> list[dict[str, Any]]:
            return [{"valid_time": VALID_TIME_1}]

    class FakeSession:
        statement = ""
        parameters: dict[str, Any] = {}

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRows:
            self.statement = str(statement)
            self.parameters = parameters
            return FakeRows()

    session = FakeSession()

    result = valid_times_for_layer(
        session,
        layer_id,
        run_id=RUN_ID,
        basin_version_id="basin_v1",
        river_network_version_id="rnv_v1",
        duration="1h",
    )

    sql = re.sub(r"\s+", " ", session.statement)
    assert result.valid_times == [_iso(VALID_TIME_1)]
    assert expected_table in sql
    for predicate in expected_identity:
        assert predicate in sql
    assert "(:run_id IS NULL OR run_id = :run_id)" not in sql
    assert "OR run_id = :run_id" not in sql
    assert "(:basin_version_id IS NULL OR basin_version_id = :basin_version_id)" not in sql
    assert "OR basin_version_id = :basin_version_id" not in sql
    assert (
        "(:river_network_version_id IS NULL OR river_network_version_id = :river_network_version_id)" not in sql
    )
    assert "OR river_network_version_id = :river_network_version_id" not in sql
    assert "ORDER BY valid_time DESC LIMIT :limit" in sql
    assert session.parameters["run_id"] == RUN_ID
    assert session.parameters["basin_version_id"] == "basin_v1"
    assert session.parameters["river_network_version_id"] == "rnv_v1"
    assert session.parameters["limit"] == MVT_VALID_TIME_SAMPLE_LIMIT + 1


@pytest.mark.parametrize("layer_id", ["flood-return-period", "warning-level", "discharge"])
def test_valid_times_for_layer_concrete_run_requires_selected_identity(layer_id: str) -> None:
    class FakeSession:
        def execute(self, *_args: Any, **_kwargs: Any) -> None:
            pytest.fail("valid-time discovery should fail before SQL without selected identity")

    with pytest.raises(ValueError, match="requires selected basin and river-network identity"):
        valid_times_for_layer(FakeSession(), layer_id, run_id=RUN_ID)


def test_valid_times_for_layer_internal_unscoped_discovery_keeps_no_nullable_run_or() -> None:
    class FakeRows:
        def mappings(self) -> "FakeRows":
            return self

        def all(self) -> list[dict[str, Any]]:
            return []

    class FakeSession:
        statement = ""

        def execute(self, statement: Any, parameters: dict[str, Any]) -> FakeRows:
            self.statement = str(statement)
            assert parameters["run_id"] is None
            return FakeRows()

    session = FakeSession()

    result = valid_times_for_layer(session, "discharge", run_id=None)

    sql = re.sub(r"\s+", " ", session.statement)
    assert result.valid_times == []
    assert "hydro.river_timeseries" in sql
    assert "run_id = :run_id" not in sql
    assert "(:run_id IS NULL OR run_id = :run_id)" not in sql


def test_layer_metadata_endpoint_scopes_cache_identity_to_explicit_run_id() -> None:
    old_run_id = "aa_metadata_endpoint_old"
    new_run_id = "zz_metadata_endpoint_new"
    old_valid_time = datetime(2026, 5, 20, 6, tzinfo=UTC)
    new_valid_time = datetime(2026, 5, 21, 6, tzinfo=UTC)
    with _store() as session:
        for run_id, valid_time in ((old_run_id, old_valid_time), (new_run_id, new_valid_time)):
            session.execute(
                text(
                    """
                    INSERT INTO hydro.hydro_run (
                        run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                        start_time, end_time, status, run_manifest_uri
                    )
                    VALUES (
                        :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                        'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "cycle_time": valid_time,
                    "start_time": valid_time,
                    "end_time": valid_time + timedelta(hours=1),
                },
            )
            _insert_result(
                session,
                "seg_001",
                "basin_v1",
                "rnv_v1",
                valid_time,
                100.0,
                2.0,
                "normal",
                False,
                run_id=run_id,
            )
            _insert_timeseries_result(session, "seg_001", run_id, valid_time, 100.0)
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                scoped = client.get(f"/api/v1/layers?run_id={old_run_id}")
                unscoped = client.get("/api/v1/layers")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert scoped.status_code == 200
    assert unscoped.status_code == 200
    scoped_metadata = {layer["layer_id"]: layer["metadata"] for layer in scoped.json()["data"]}
    unscoped_metadata = {layer["layer_id"]: layer["metadata"] for layer in unscoped.json()["data"]}
    # Flood/warning keep single-run cache identity for both scoped and unscoped requests.
    for layer_id in ("flood-return-period", "warning-level"):
        assert scoped_metadata[layer_id]["source_refs"]["run_id"] == old_run_id
        assert unscoped_metadata[layer_id]["source_refs"]["run_id"] == new_run_id
        assert scoped_metadata[layer_id]["source_refs"]["basin_version_id"] == "basin_v1"
        assert scoped_metadata[layer_id]["source_refs"]["river_network_version_id"] == "rnv_v1"
        assert unscoped_metadata[layer_id]["source_refs"]["basin_version_id"] == "basin_v1"
        assert unscoped_metadata[layer_id]["source_refs"]["river_network_version_id"] == "rnv_v1"
        assert scoped_metadata[layer_id]["cache_version"] != unscoped_metadata[layer_id]["cache_version"]
        assert scoped_metadata[layer_id]["source_refs"]["source_version"] != unscoped_metadata[layer_id]["source_refs"][
            "source_version"
        ]
    # Discharge is national in BOTH the scoped and unscoped catalogs (spec invariant
    # *Default discharge tile URL is national across all `/api/v1/layers` callers*):
    # source_refs is empty either way, the tile URL template never carries a {run_id}
    # placeholder, and the per-layer ETag is byte-identical across the two call shapes.
    assert scoped_metadata["discharge"]["source_refs"] == {}
    assert unscoped_metadata["discharge"]["source_refs"] == {}
    assert scoped_metadata["discharge"]["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert unscoped_metadata["discharge"]["tile_url_template"] == (
        "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
    )
    assert "{run_id}" not in scoped_metadata["discharge"]["tile_url_template"]
    assert scoped_metadata["discharge"]["cache_version"] == unscoped_metadata["discharge"]["cache_version"]


def test_flood_layer_valid_times_default_to_one_hour_duration_identity() -> None:
    run_id = "duration_identity_run"
    start = datetime(2026, 5, 22, tzinfo=UTC)
    latest_1h = start + timedelta(hours=1)
    later_24h = start + timedelta(hours=24)
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": start,
                "start_time": start,
                "end_time": later_24h,
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            latest_1h,
            100.0,
            2.0,
            "normal",
            False,
            run_id=run_id,
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            later_24h,
            200.0,
            10.0,
            "watch",
            False,
            run_id=run_id,
            duration="24h",
        )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                default_flood = client.get(f"/api/v1/layers/flood-return-period/valid-times?run_id={run_id}")
                default_warning = client.get(f"/api/v1/layers/warning-level/valid-times?run_id={run_id}")
                explicit_24h = client.get(
                    f"/api/v1/layers/flood-return-period/valid-times?run_id={run_id}&duration=24h"
                )
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert default_flood.status_code == 200
    assert default_flood.json()["data"]["valid_times"] == [_iso(latest_1h)]
    assert default_warning.status_code == 200
    assert default_warning.json()["data"]["valid_times"] == [_iso(latest_1h)]
    assert explicit_24h.status_code == 200
    assert explicit_24h.json()["data"]["valid_times"] == [_iso(later_24h)]


def test_hydro_valid_time_discovery_excludes_sibling_only_selected_identity() -> None:
    layer_id = "discharge"
    run_id = "valid_time_hydro_identity_run"
    selected_time = datetime(2026, 5, 23, 6, tzinfo=UTC)
    sibling_only_time = datetime(2026, 5, 23, 12, tzinfo=UTC)
    variable = "q_down"
    value = 150.0
    unit = "m3/s"
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": selected_time,
                "start_time": selected_time,
                "end_time": sibling_only_time,
            },
        )
        _insert_timeseries_result(session, "seg_001", run_id, selected_time, value, variable=variable, unit=unit)
        session.execute(
            text(
                """
                INSERT INTO hydro.river_timeseries (
                    run_id, basin_version_id, river_network_version_id, river_segment_id,
                    valid_time, variable, value, unit
                )
                VALUES (
                    :run_id, 'basin_v2', 'rnv_v2', 'seg_004',
                    :valid_time, :variable, 999.0, :unit
                )
                """
            ),
            {"run_id": run_id, "valid_time": sibling_only_time, "variable": variable, "unit": unit},
        )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                valid_times_response = client.get(f"/api/v1/layers/{layer_id}/valid-times?run_id={run_id}")
                catalog_response = client.get(f"/api/v1/layers?run_id={run_id}")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert valid_times_response.status_code == 200
    assert valid_times_response.json()["data"]["valid_times"] == [_iso(selected_time)]
    assert catalog_response.status_code == 200
    metadata = {layer["layer_id"]: layer["metadata"] for layer in catalog_response.json()["data"]}
    assert metadata[layer_id]["valid_times"] == [_iso(selected_time)]
    assert _iso(sibling_only_time) not in metadata[layer_id]["valid_times"]


@pytest.mark.parametrize("layer_id", ["flood-return-period", "warning-level"])
def test_flood_valid_time_discovery_excludes_sibling_only_selected_identity(
    layer_id: str,
) -> None:
    run_id = "valid_time_flood_identity_run"
    selected_time = datetime(2026, 5, 24, 6, tzinfo=UTC)
    sibling_only_time = datetime(2026, 5, 24, 12, tzinfo=UTC)
    with _store() as session:
        session.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "cycle_time": selected_time,
                "start_time": selected_time,
                "end_time": sibling_only_time,
            },
        )
        _insert_result(
            session,
            "seg_001",
            "basin_v1",
            "rnv_v1",
            selected_time,
            100.0,
            2.0,
            "normal",
            False,
            run_id=run_id,
        )
        _insert_result(
            session,
            "seg_004",
            "basin_v2",
            "rnv_v2",
            sibling_only_time,
            999.0,
            99.0,
            "severe",
            False,
            run_id=run_id,
        )
        session.commit()
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                valid_times_response = client.get(f"/api/v1/layers/{layer_id}/valid-times?run_id={run_id}")
                catalog_response = client.get(f"/api/v1/layers?run_id={run_id}")
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert valid_times_response.status_code == 200
    assert valid_times_response.json()["data"]["valid_times"] == [_iso(selected_time)]
    assert catalog_response.status_code == 200
    metadata = {layer["layer_id"]: layer["metadata"] for layer in catalog_response.json()["data"]}
    assert metadata[layer_id]["valid_times"] == [_iso(selected_time)]
    assert _iso(sibling_only_time) not in metadata[layer_id]["valid_times"]


def test_hydro_layer_metadata_declares_public_cache_identity_and_legacy_aliases() -> None:
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    layers = {layer["layer_id"]: layer for layer in response.json()["data"]}
    discharge = layers["discharge"]["metadata"]
    assert discharge["cache_layer_id"] == "discharge"
    assert discharge["route_variable"] == "q_down"
    assert discharge["legacy_layer_ids"] == ["hydro:q_down"]


def test_layer_metadata_required_placeholders_resolve_from_source_refs_or_route_constants() -> None:
    documented_route_constants = {"z", "x", "y", "valid_time"}
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    for layer in response.json()["data"]:
        metadata = layer["metadata"]
        if metadata.get("tile_format") != "mvt":
            continue
        source_refs = metadata.get("source_refs") or {}
        missing = [
            placeholder
            for placeholder in metadata.get("required_placeholders", [])
            if not source_refs.get(placeholder) and placeholder not in documented_route_constants
        ]
        assert missing == []

    river_network = next(layer["metadata"] for layer in response.json()["data"] if layer["layer_id"] == "river-network")
    assert river_network["source_refs"]["basin_version_id"] == "basin_v1"
    metadata = {layer["layer_id"]: layer["metadata"] for layer in response.json()["data"]}
    assert metadata["flood-return-period"]["source_refs"]["duration"] == "1h"
    assert metadata["warning-level"]["source_refs"]["duration"] == "1h"
    # Unscoped discharge is national: run-less template, no single-run source refs.
    assert metadata["discharge"]["source_refs"] == {}
    assert metadata["discharge"]["required_placeholders"] == ["valid_time", "z", "x", "y"]
    for layer_id in ("flood-return-period", "warning-level", "river-network"):
        assert metadata[layer_id]["source_refs"]["basin_version_id"] == "basin_v1"
        assert metadata[layer_id]["source_refs"]["river_network_version_id"] == "rnv_v1"
    assert (
        river_network["url_template"]
        .replace("{basin_version_id}", river_network["source_refs"]["basin_version_id"])
        .replace("{z}", "0")
        .replace("{x}", "0")
        .replace("{y}", "0")
        == "/api/v1/tiles/river-network/basin_v1/0/0/0.pbf"
    )


def test_warning_level_metadata_declares_flood_return_period_alias_identity() -> None:
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    metadata = {layer["layer_id"]: layer["metadata"] for layer in response.json()["data"]}
    warning = metadata["warning-level"]
    canonical = metadata["flood-return-period"]
    assert warning["cache_layer_id"] == "flood-return-period"
    assert warning["canonical_route_layer_id"] == "flood-return-period"
    assert warning["alias_of"] == "flood-return-period"
    assert warning["alias_semantic"] == "style_layer"
    assert warning["route_variable"] == "return_period"
    assert warning["url_template"] == canonical["url_template"]
    assert warning["maplibre_source_layer"] == canonical["maplibre_source_layer"]


def test_layer_metadata_cache_identity_changes_with_latest_source_refs_and_valid_times() -> None:
    old_run_id = "aa_metadata_identity_old"
    new_run_id = "zz_metadata_identity_new"
    old_valid_time = datetime(2026, 5, 20, 6, tzinfo=UTC)
    new_valid_time = datetime(2026, 5, 21, 6, tzinfo=UTC)
    with _store() as session:
        for run_id, valid_time in ((old_run_id, old_valid_time), (new_run_id, new_valid_time)):
            session.execute(
                text(
                    """
                    INSERT INTO hydro.hydro_run (
                        run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                        start_time, end_time, status, run_manifest_uri
                    )
                    VALUES (
                        :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                        'GFS', :cycle_time, :start_time, :end_time, 'frequency_done', 'object://manifest'
                    )
                    """
                ),
                {
                    "run_id": run_id,
                    "cycle_time": valid_time,
                    "start_time": valid_time,
                    "end_time": valid_time + timedelta(hours=1),
                },
            )
            _insert_result(
                session,
                "seg_001",
                "basin_v1",
                "rnv_v1",
                valid_time,
                100.0,
                2.0,
                "normal",
                False,
                run_id=run_id,
            )
            _insert_timeseries_result(session, "seg_001", run_id, valid_time, 100.0)
        session.commit()

        def catalog_for(run_id: str) -> dict[str, dict[str, Any]]:
            return {
                layer.layer_id: layer.metadata or {}
                for layer in flood_alert_routes._default_layer_catalog(
                    session,
                    run_id=run_id,
                    source_version="rnv_v1",
                    basin_version_id="basin_v1",
                    river_network_version_id="rnv_v1",
                )
            }

        old_catalog = catalog_for(old_run_id)
        new_catalog = catalog_for(new_run_id)

    # Flood / warning layers are per-run: cache_version + valid_times rotate with the caller's run_id.
    for layer_id in ("flood-return-period", "warning-level"):
        old_metadata = old_catalog[layer_id]
        new_metadata = new_catalog[layer_id]
        assert old_metadata["source_refs"]["source_version"] == new_metadata["source_refs"]["source_version"]
        assert old_metadata["source_refs"]["run_id"] == old_run_id
        assert new_metadata["source_refs"]["run_id"] == new_run_id
        assert old_metadata["source_refs"]["basin_version_id"] == "basin_v1"
        assert new_metadata["source_refs"]["basin_version_id"] == "basin_v1"
        assert old_metadata["source_refs"]["river_network_version_id"] == "rnv_v1"
        assert new_metadata["source_refs"]["river_network_version_id"] == "rnv_v1"
        assert old_metadata["valid_times"] != new_metadata["valid_times"]
        assert old_metadata["cache_version"] != new_metadata["cache_version"]
        assert old_metadata["cache_etag"] != new_metadata["cache_etag"]

    # Discharge is national (spec invariant *Discharge catalog cache identity is run-agnostic*):
    # both catalog calls observe the same `national_discharge_valid_times(session)` snapshot
    # (taken after all inserts committed), so metadata is byte-identical across the two calls,
    # regardless of the caller's run_id. source_refs is empty either way.
    assert old_catalog["discharge"]["source_refs"] == {}
    assert new_catalog["discharge"]["source_refs"] == {}
    assert old_catalog["discharge"]["valid_times"] == new_catalog["discharge"]["valid_times"]
    assert old_catalog["discharge"]["cache_version"] == new_catalog["discharge"]["cache_version"]
    assert old_catalog["discharge"]["cache_etag"] == new_catalog["discharge"]["cache_etag"]


def test_advertised_mvt_layer_cache_identity_matches_route_or_declared_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_inputs = {
        "flood-return-period": flood_alert_routes.TileInput(
            layer_id="flood-return-period",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="duration:1h",
        ),
        "warning-level": flood_alert_routes.TileInput(
            layer_id="flood-return-period",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="duration:1h",
        ),
        "discharge": flood_alert_routes.TileInput(
            layer_id="discharge",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="variable:q_down",
        ),
        "river-network": flood_alert_routes.TileInput(
            layer_id="river-network",
            source_id="basin_v1",
            source_version=RIVER_NETWORK_SOURCE_VERSION_V1,
            valid_time=None,
            z=6,
            x=12,
            y=24,
        ),
    }
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        with TestClient(app) as client:
            app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
            try:
                response = client.get("/api/v1/layers")
            finally:
                app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)
        layers = {layer["layer_id"]: layer["metadata"] for layer in response.json()["data"]}
        for layer_id, tile_input in route_inputs.items():
            metadata = layers[layer_id]
            advertised_run_id = metadata["source_refs"].get("run_id")
            if layer_id == "discharge" and advertised_run_id is None:
                # Unscoped discharge is national (run-less); its cache identity is keyed by
                # the fixed national source id, not an advertised run_id. Live tile-byte and
                # cache verification for this layer happens on node-27 real Postgres.
                assert metadata["tile_url_template"] == (
                    "/api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf"
                )
                continue
            route_tile_input = (
                flood_alert_routes.TileInput(
                    layer_id=tile_input.layer_id,
                    source_id=advertised_run_id,
                    source_version=tile_input.source_version,
                    valid_time=tile_input.valid_time,
                    z=tile_input.z,
                    x=tile_input.x,
                    y=tile_input.y,
                    style_id=tile_input.style_id,
                    variant_id=tile_input.variant_id,
                    schema_version=tile_input.schema_version,
                    encoder_version=tile_input.encoder_version,
                )
                if layer_id != "river-network"
                else tile_input
            )
            receipt = flood_alert_routes.build_raw_tile_response(
                session,
                _with_route_source_version(session, route_tile_input),
                f"{layer_id}-tile".encode(),
            )
            expected_layer_id = metadata["cache_layer_id"]
            assert receipt.layer_id == expected_layer_id
            if layer_id != "river-network":
                source_version = flood_alert_routes._run_source_version(
                    flood_alert_routes._require_run(session, advertised_run_id)
                )
                assert metadata["source_refs"]["source_version"] == source_version
            assert session.execute(
                text("SELECT COUNT(*) FROM map.tile_layer WHERE layer_id = :layer_id"),
                {"layer_id": expected_layer_id},
            ).scalar_one() == 1
            assert session.execute(
                text("SELECT COUNT(*) FROM map.tile_cache WHERE layer_id = :layer_id"),
                {"layer_id": expected_layer_id},
            ).scalar_one() >= 1
            if metadata.get("alias_of"):
                assert metadata["alias_of"] == expected_layer_id


def test_mvt_postgis_sql_shape_documents_tile_envelope_transform_and_encoding() -> None:
    statement = flood_alert_routes.postgis_tile_sql("flood-return-period")

    assert "ST_TileEnvelope(:z, :x, :y)" in statement
    assert "ST_Transform(eligible.geom, 3857)" in statement
    assert "ST_AsMVTGeom" in statement
    assert statement.index("ST_SimplifyPreserveTopology") < statement.index("ST_AsMVTGeom")
    assert "ST_MakeValid(ST_Transform(eligible.geom, 3857))" in statement
    assert ":simplification_tolerance_m" in statement
    assert "extent => 4096" in statement
    assert "buffer => 64" in statement
    assert "clip_geom => true" in statement
    assert "ST_AsMVT(tile_rows, 'flood_return_period', 4096, 'mvt_geom')" in statement
    assert "feature_count <= :feature_limit" in statement
    assert "coordinate_count <= :collection_coordinate_limit" in statement
    assert "budget_gate AS" in statement
    assert "CROSS JOIN budget_gate" in statement
    assert "bounded_rows AS" in statement
    assert "source_identity_stats AS" in statement
    assert "source_stats AS" in statement
    assert "FROM source_identity_stats, source_stats, budget_stats, prefilter_stats" in statement


def test_mvt_postgis_sql_shape_simplifies_all_production_layers_before_encoding() -> None:
    for layer in ("flood-return-period", "hydro", "river-network", "met-stations"):
        statement = flood_alert_routes.postgis_tile_sql(layer)
        assert "eligible AS" in statement
        assert "simplified AS" in statement
        assert "ST_SimplifyPreserveTopology" in statement
        assert "ST_MakeValid(ST_Transform(eligible.geom, 3857))" in statement
        assert "simplified.geom_3857" in statement
        assert statement.index("eligible AS") < statement.index("budget_gate AS") < statement.index("simplified AS")
        assert statement.index("budget_gate AS") < statement.index("clipped AS")
        assert ":simplification_tolerance_m" in statement


def test_mvt_postgis_sql_shape_short_circuits_tile_budgets_before_expensive_geometry_work() -> None:
    for layer in ("flood-return-period", "hydro", "river-network", "met-stations"):
        statement = flood_alert_routes.postgis_tile_sql(layer)
        budget_gate_index = statement.index("budget_gate AS")
        expensive_indexes = [
            statement.index("ST_MakeValid"),
            statement.index("ST_Transform(eligible.geom, 3857)"),
            statement.index("ST_SimplifyPreserveTopology"),
            statement.index("ST_AsMVTGeom"),
        ]
        assert "WHERE source_coordinate_count <= :feature_coordinate_limit" in statement
        assert "AND source_coordinate_dimensions <= :max_coordinate_dimensions" in statement
        assert "budget_stats AS" in statement
        assert "FROM eligible" in statement[statement.index("budget_stats AS") : statement.index("budget_gate AS")]
        assert "budget_stats.feature_count <= :feature_limit" in statement
        assert "budget_stats.coordinate_count <= :collection_coordinate_limit" in statement
        assert all(budget_gate_index < expensive_index for expensive_index in expensive_indexes)
        assert statement.index("prefilter_stats AS") < budget_gate_index < statement.index("simplified AS")


def test_mvt_postgis_sql_bounds_source_rows_before_reusable_aggregates() -> None:
    for layer in ("flood-return-period", "hydro", "river-network", "met-stations"):
        statement = flood_alert_routes.postgis_tile_sql(layer)
        sql = re.sub(r"\s+", " ", statement)
        bounded_cte = sql[sql.index("bounded_rows AS") : sql.index("source_stats AS")]
        source_stats_cte = sql[sql.index("source_stats AS") : sql.index("eligible AS")]
        prefilter_cte = sql[sql.index("prefilter_stats AS") : sql.index("budget_stats AS")]

        assert "source_rows AS NOT MATERIALIZED" in sql
        assert "source_identity_stats AS" in sql
        assert "source_identity_count" in sql
        assert "FROM source_rows, bounds" in bounded_cte
        assert "source_rows.geom && ST_Transform(bounds.geom_3857, 4490)" in bounded_cte
        assert sql.index("source_identity_stats AS") < sql.index("bounded_rows AS")
        assert sql.index("bounded_rows AS") < sql.index("source_stats AS") < sql.index("budget_stats AS")
        assert "EXISTS (SELECT 1 FROM source_rows)" in sql[
            sql.index("source_identity_stats AS") : sql.index("bounded_rows AS")
        ]
        assert "EXISTS (SELECT 1 FROM bounded_rows)" in source_stats_cte
        assert "FROM bounded_rows" in source_stats_cte
        assert "FROM bounded_rows" in prefilter_cte
        assert "FROM source_rows" not in source_stats_cte
        assert "FROM source_rows" not in prefilter_cte
        assert sql.count("FROM source_rows") == 2
        assert sql.index("source_rows.geom && ST_Transform(bounds.geom_3857, 4490)") < sql.index("source_stats AS")
        assert sql.index("bounded_rows AS") < sql.index("ST_MakeValid")


def test_mvt_postgis_tile_params_bind_zoom_safe_simplification_tolerance() -> None:
    low_zoom = flood_alert_routes._postgis_tile_params({}, z=0, x=0, y=0)["simplification_tolerance_m"]
    high_zoom = flood_alert_routes._postgis_tile_params({}, z=14, x=0, y=0)["simplification_tolerance_m"]

    assert low_zoom == 256.0
    assert high_zoom == 0.5


def test_mvt_postgis_sql_shape_projects_metadata_properties_and_bindable_casts() -> None:
    for layer in ("flood-return-period", "hydro", "river-network"):
        statement = flood_alert_routes.postgis_tile_sql(layer)
        sql = re.sub(r"\s+", " ", statement)
        assert "feature_id" in sql
        assert " AS segment_id" in sql
        assert "river_segment_id" in sql
        assert "river_network_version_id" in sql
        assert ":basin_version_id::text" not in statement

    assert "CAST(:basin_version_id AS text) AS basin_version_id" in flood_alert_routes.postgis_tile_sql(
        "river-network"
    )
    assert "r.q_value AS value" in flood_alert_routes.postgis_tile_sql("flood-return-period")
    assert "ts.value" in flood_alert_routes.postgis_tile_sql("hydro")


def test_station_mvt_postgis_sql_uses_station_inventory_source_layer_and_properties() -> None:
    statement = flood_alert_routes.postgis_tile_sql("met-stations")
    sql = re.sub(r"\s+", " ", statement)
    source_cte = sql[sql.index("source_rows AS") : sql.index("bounded_rows AS")]
    projected_columns = _mvt_tile_projection(sql)

    assert "FROM met.met_station ms" in source_cte
    assert "WHERE ms.basin_version_id = :basin_version_id" in source_cte
    assert "AND ms.active_flag = true" in source_cte
    assert "COALESCE(ms.station_name, '') AS station_name" in source_cte
    assert "ST_AsMVTGeom" in statement
    assert "ST_AsMVT(tile_rows, 'met_stations', 4096, 'mvt_geom')" in statement
    assert "station_id IS NULL OR station_id::text = ''" in statement
    assert "basin_version_id IS NULL OR basin_version_id::text = ''" in statement
    assert "station_role IS NULL OR station_role::text = ''" in statement
    assert "active_flag IS NULL" in statement
    assert "ORDER BY station_id" in sql
    assert projected_columns == (
        "station_id",
        "basin_version_id",
        "station_name",
        "station_role",
        "active_flag",
        "mvt_geom",
    )
    assert "properties_json" not in projected_columns
    assert "elevation_m" not in projected_columns


def test_mvt_postgis_sql_projects_source_time_identity_through_public_allowlist() -> None:
    hydro = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("hydro"))
    flood = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("flood-return-period"))

    hydro_source_cte = hydro[hydro.index("source_rows AS") : hydro.index("bounded_rows AS")]
    hydro_tile_projection = _mvt_tile_projection(hydro)
    assert "ts.run_id" in hydro_source_cte
    assert "ts.variable" in hydro_source_cte
    assert "to_char(ts.valid_time AT TIME ZONE 'UTC'" in hydro_source_cte
    assert "valid_time" in hydro_tile_projection
    assert "run_id" in hydro_tile_projection
    assert "variable" in hydro_tile_projection
    assert hydro_tile_projection.index("run_id") < hydro_tile_projection.index("valid_time")

    flood_source_cte = flood[flood.index("source_rows AS") : flood.index("bounded_rows AS")]
    flood_tile_projection = _mvt_tile_projection(flood)
    assert "r.run_id" in flood_source_cte
    assert "r.duration" in flood_source_cte
    assert "to_char(r.valid_time AT TIME ZONE 'UTC'" in flood_source_cte
    assert "valid_time" in flood_tile_projection
    assert "run_id" in flood_tile_projection
    assert "duration" in flood_tile_projection
    assert flood_tile_projection.index("run_id") < flood_tile_projection.index("valid_time")


def test_mvt_postgis_sql_filters_run_scoped_sources_by_selected_basin_and_network_identity() -> None:
    hydro = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("hydro"))
    flood = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("flood-return-period"))

    hydro_source_cte = hydro[hydro.index("source_rows AS") : hydro.index("bounded_rows AS")]
    flood_source_cte = flood[flood.index("source_rows AS") : flood.index("bounded_rows AS")]
    assert "WHERE ts.run_id = :run_id" in hydro_source_cte
    assert "AND ts.basin_version_id = :basin_version_id" in hydro_source_cte
    assert "AND ts.river_network_version_id = :river_network_version_id" in hydro_source_cte
    assert hydro_source_cte.index("ts.basin_version_id = :basin_version_id") < hydro_source_cte.index(
        "ts.variable = :variable"
    )
    assert "WHERE r.run_id = :run_id" in flood_source_cte
    assert "AND r.basin_version_id = :basin_version_id" in flood_source_cte
    assert "AND r.river_network_version_id = :river_network_version_id" in flood_source_cte
    assert flood_source_cte.index("r.basin_version_id = :basin_version_id") < flood_source_cte.index(
        "r.duration = :duration"
    )


def test_run_source_version_includes_same_basin_and_network_identity_as_preflight_and_sql_params() -> None:
    with _store() as session:
        run = flood_alert_routes._require_frequency_ready(session, RUN_ID)
        source_version = flood_alert_routes._run_source_version(run)
        basin_version_id, river_network_version_id = flood_alert_routes._require_run_source_identity(
            run, layer_id="discharge"
        )
        flood_alert_routes._require_hydro_mvt_source_identity(
            session,
            run_id=RUN_ID,
            variable="q_down",
            valid_time=VALID_TIME_1,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
        )
        flood_alert_routes._require_flood_mvt_source_identity(
            session,
            run_id=RUN_ID,
            duration="1h",
            valid_time=VALID_TIME_1,
            basin_version_id=basin_version_id,
            river_network_version_id=river_network_version_id,
        )
        hydro_params = flood_alert_routes._postgis_tile_params(
            {
                "run_id": RUN_ID,
                "variable": "q_down",
                "valid_time": VALID_TIME_1,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
            },
            z=6,
            x=12,
            y=24,
        )
        flood_params = flood_alert_routes._postgis_tile_params(
            {
                "run_id": RUN_ID,
                "duration": "1h",
                "valid_time": VALID_TIME_1,
                "basin_version_id": basin_version_id,
                "river_network_version_id": river_network_version_id,
            },
            z=6,
            x=12,
            y=24,
        )

    assert basin_version_id == run["basin_version_id"] == "basin_v1"
    assert river_network_version_id == run["river_network_version_id"] == "rnv_v1"
    assert source_version.startswith("rnv_v1;run-revision:")
    assert hydro_params["basin_version_id"] == flood_params["basin_version_id"] == "basin_v1"
    assert hydro_params["river_network_version_id"] == flood_params["river_network_version_id"] == "rnv_v1"


def test_layer_metadata_property_schema_declares_public_source_time_identity() -> None:
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    metadata = {layer["layer_id"]: layer["metadata"] for layer in response.json()["data"]}

    required = metadata["discharge"]["property_schema"]["required"]
    assert {"run_id", "variable", "valid_time"}.issubset(required)
    assert "duration" not in required

    for layer_id in ("flood-return-period", "warning-level"):
        required = metadata[layer_id]["property_schema"]["required"]
        assert {"run_id", "duration", "valid_time"}.issubset(required)
        assert "variable" not in required


def test_openapi_hydro_mvt_variable_enum_is_tightened_to_discharge_only() -> None:
    """OpenAPI HydroMvtVariable enum MUST NOT readmit the retired hydro variant.

    Pins the BREAKING contract behind the catalog deletion (epic #579 / PR #580).
    Spec scenario `water_level variable is rejected at the backend boundary`.
    """
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    enum_values = spec["components"]["parameters"]["HydroMvtVariable"]["schema"]["enum"]
    assert enum_values == ["q_down"]
    # Drift sentinel: anything that re-adds the retired variant should fail loudly.
    forbidden = "wat" + "er_level"
    assert forbidden not in enum_values


def test_layers_catalog_advertises_four_layers_without_retired_hydro_variant() -> None:
    """Catalog deletion regression: `/api/v1/layers` must advertise exactly the
    canonical 4-layer hydrology/base set after the retired hydro variant was removed.
    """
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    layer_ids = {layer["layer_id"] for layer in response.json()["data"]}
    assert layer_ids == {"discharge", "flood-return-period", "warning-level", "river-network"}
    # Sanity: the retired hydro variant must not reappear via metadata
    # (cache_layer_id / route_variable / legacy_layer_ids backdoors).
    forbidden_layer_id = "wat" + "er-level"
    forbidden_variable = "wat" + "er_level"
    assert forbidden_layer_id not in layer_ids
    for layer in response.json()["data"]:
        metadata = layer["metadata"] or {}
        assert metadata.get("cache_layer_id") != forbidden_layer_id
        assert metadata.get("route_variable") != forbidden_variable
        assert forbidden_layer_id not in (metadata.get("legacy_layer_ids") or [])


@pytest.mark.parametrize(
    "layer_variant",
    [
        "wat" + "er-level",  # canonical lowercase id removed from catalog
        ("wat" + "er-level").upper(),  # WATER-LEVEL — SAFE_TILE_IDENTIFIER_RE permits A-Z
        "Water-Level",  # mixed case path
    ],
)
def test_layer_valid_times_for_retired_hydro_layer_returns_422(layer_variant: str) -> None:
    """`GET /api/v1/layers/<retired-layer>/valid-times` MUST return 422 at the
    backend boundary now that the catalog removed the layer (#580). Covers
    spec scenario `water_level variable is rejected at the backend boundary`,
    including case-variant paths — `SUPPORTED_PUBLIC_LAYER_IDS` is a
    case-sensitive frozenset and `SAFE_TILE_IDENTIFIER_RE` admits A-Z, so
    uppercase / mixed-case spellings must also fail at the 422 deny gate
    (not 200, not 404).
    """
    with _client() as client:
        response = client.get(f"/api/v1/layers/{layer_variant}/valid-times")

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["layer_id"] == layer_variant
    supported = body["error"]["details"]["supported"]
    assert set(supported) == {"discharge", "flood-return-period", "warning-level", "river-network"}


def test_supported_public_layer_ids_matches_default_catalog() -> None:
    """Drift sentinel: `SUPPORTED_PUBLIC_LAYER_IDS` (used by the valid-times
    422 gate) MUST stay in lockstep with `_PUBLIC_LAYER_DEFINITIONS` (consumed
    by `_default_layer_catalog`). Both are derived from the single
    `_PUBLIC_LAYER_DEFINITIONS` literal; this test pins that derivation so
    that a future 5th-layer addition cannot silently 422-reject the new layer
    if someone edits one side without the other.
    """
    catalog_layer_ids = {
        definition[0] for definition in flood_alert_routes._PUBLIC_LAYER_DEFINITIONS
    }
    assert flood_alert_routes.SUPPORTED_PUBLIC_LAYER_IDS == catalog_layer_ids
    # Pin the canonical 4-layer set explicitly so neither side can drop a
    # layer without updating tests too.
    assert catalog_layer_ids == {
        "discharge",
        "flood-return-period",
        "warning-level",
        "river-network",
    }


def test_cache_layer_metadata_rejects_retired_hydro_variant() -> None:
    """Defense-in-depth: `_cache_layer_metadata`'s `hydro:` fallback MUST
    refuse to synthesize a tile URI / cache row for any hydrology variant
    outside `SUPPORTED_HYDRO_MVT_VARIABLES`, including the retired
    `water_level` id. Today every route validates `variable` first, but a
    future CLI / worker / debug `TileInput(layer_id="hydro:<retired>")`
    must not silently rebuild the legacy URI shape.
    """
    from services.tiles.mvt import (
        SUPPORTED_HYDRO_MVT_VARIABLES,
        TileInput,
        _cache_layer_metadata,
    )

    retired_layer_id = "hydro:" + "wat" + "er_level"
    synthetic_tile = TileInput(
        layer_id=retired_layer_id,
        source_id="run_synthetic",
        source_version="synthetic",
        valid_time=VALID_TIME_1_ISO,
        z=6,
        x=12,
        y=24,
        variant_id="variable:" + "wat" + "er_level",
    )
    with pytest.raises(ValueError) as excinfo:
        _cache_layer_metadata(synthetic_tile)
    message = str(excinfo.value)
    assert retired_layer_id in message
    # Allow-list must be referenced in the error for actionable debugging.
    for variable in SUPPORTED_HYDRO_MVT_VARIABLES:
        assert variable in message

    # Sanity: the canonical allow-listed hydro variants must still pass.
    for canonical_variable in SUPPORTED_HYDRO_MVT_VARIABLES:
        canonical_tile = TileInput(
            layer_id=f"hydro:{canonical_variable}",
            source_id="run_synthetic",
            source_version="synthetic",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id=f"variable:{canonical_variable}",
        )
        metadata = _cache_layer_metadata(canonical_tile)
        assert metadata["variable"] == canonical_variable
        assert metadata["layer_type"] == "hydrological_output"


def test_runs_does_not_require_flood_product_ready_for_discharge() -> None:
    """`GET /api/v1/runs?source=best` (no `flood_product_ready` filter) MUST return
    frequency-ready runs even when their flood frequency products are incomplete
    (mirrors QHH/Heihe in production: q_down present, return_period_result absent).

    Pins the cross-PR contract that the frontend default discharge path no longer
    needs to gate run selection on `flood_product_ready=true`. The route MUST NOT
    auto-apply the filter; only the explicit `?flood_product_ready=true` filter
    activates the strict gate (covered separately by the forecast_api tests).
    """

    class _StubRunStore:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def list_runs(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            # When flood_product_ready is not set, store returns frequency-ready
            # runs irrespective of return-period readiness. We model that exactly:
            # status='frequency_done', no flood return-period product attached.
            return {
                "total_count": 1,
                "items": [
                    {
                        "run_id": "frequency_ready_flood_incomplete_run",
                        "run_type": "forecast",
                        "scenario_id": "forecast_gfs_deterministic",
                        "model_id": "model_qhh",
                        "basin_version_id": "basins_qhh_vbasins",
                        "river_network_version_id": "basins_qhh_rivnet_vbasins",
                        "forcing_version_id": None,
                        "init_state_id": None,
                        "source_id": "GFS",
                        "cycle_time": "2026-05-07T00:00:00Z",
                        "status": "frequency_done",
                        "slurm_job_id": None,
                        "start_time": "2026-05-07T00:00:00Z",
                        "end_time": "2026-05-14T00:00:00Z",
                        "run_manifest_uri": "object://manifest",
                        "output_uri": None,
                        "log_uri": None,
                        "error_code": None,
                        "error_message": None,
                        "product_quality": {
                            "flood_return_period": {
                                "quality_state": "unavailable",
                                "quality_source": "explicit",
                                "max_over_window": False,
                                "result_rows": 0,
                                "return_period_rows": 0,
                                "warning_rows": 0,
                                "expected_result_rows": 0,
                                "expected_max_result_rows": 0,
                                "expected_timestep_result_rows": 0,
                                "meaningful_result_rows": 0,
                                "meaningful_max_result_rows": 0,
                                "meaningful_timestep_result_rows": 0,
                                "no_frequency_curve_rows": 0,
                                "no_usable_frequency_curve_rows": 0,
                                "warning_threshold_unavailable_rows": 0,
                                "unavailable_products": ["return_period_result"],
                                "residual_blockers": [],
                            }
                        },
                        "created_at": "2026-05-07T00:00:00Z",
                        "updated_at": "2026-05-07T00:00:00Z",
                    }
                ],
                "limit": kwargs["limit"],
                "offset": kwargs["offset"],
            }

    store = _StubRunStore()
    app.dependency_overrides[get_forecast_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs?source=best")
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    data = body["data"]
    assert data["total_count"] == 1
    assert data["items"][0]["run_id"] == "frequency_ready_flood_incomplete_run"
    assert data["items"][0]["status"] == "frequency_done"
    # The route MUST forward `flood_product_ready` unchanged; without the explicit
    # filter the store call must NOT silently coerce it to True.
    assert store.calls[-1]["flood_product_ready"] is None
    assert store.calls[-1]["source"] == "best"


def test_river_network_mvt_sql_scopes_basin_without_model_instance_cardinality_multiply() -> None:
    statement = flood_alert_routes.postgis_tile_sql("river-network")
    sql = re.sub(r"\s+", " ", statement)

    source_cte = sql[sql.index("source_rows AS") : sql.index("bounded_rows AS")]
    assert "WHERE EXISTS ( SELECT 1 FROM core.river_network_version rnv" in source_cte
    assert "rnv.river_network_version_id = rs.river_network_version_id" in source_cte
    assert "rnv.basin_version_id = :basin_version_id" in source_cte
    assert "core.model_instance" not in source_cte
    assert "ORDER BY river_network_version_id, river_segment_id" in sql


def test_mvt_postgis_sql_shape_encodes_only_public_property_allowlist() -> None:
    expected_columns = {
        "river-network": (
            "segment_id",
            "river_segment_id",
            "river_network_version_id",
            "basin_version_id",
            "mvt_geom",
        ),
        "hydro": (
            "feature_id",
            "segment_id",
            "river_segment_id",
            "river_network_version_id",
            "basin_version_id",
            "value",
            "unit",
            "quality_flag",
            "run_id",
            "variable",
            "valid_time",
            "mvt_geom",
        ),
        "flood-return-period": (
            "feature_id",
            "segment_id",
            "river_segment_id",
            "river_network_version_id",
            "basin_version_id",
            "value",
            "unit",
            "quality_flag",
            "return_period",
            "warning_level",
            "run_id",
            "duration",
            "valid_time",
            "mvt_geom",
        ),
        "met-stations": (
            "station_id",
            "basin_version_id",
            "station_name",
            "station_role",
            "active_flag",
            "mvt_geom",
        ),
    }
    forbidden_columns = {
        "properties_json",
        "source_coordinate_count",
        "source_coordinate_dimensions",
        "feature_count",
        "coordinate_count",
    }

    for layer, columns in expected_columns.items():
        statement = flood_alert_routes.postgis_tile_sql(layer)
        sql = re.sub(r"\s+", " ", statement)
        projected_columns = _mvt_tile_projection(sql)
        projection = ", ".join(projected_columns)
        assert projected_columns == columns
        assert "*" not in projection
        assert forbidden_columns.isdisjoint(projected_columns)


def _mvt_tile_projection(sql: str) -> tuple[str, ...]:
    tile_subquery = sql[sql.index("SELECT ST_AsMVT") : sql.index(") AS tile,")]
    projection_start = tile_subquery.index("FROM ( SELECT ") + len("FROM ( SELECT ")
    projection = tile_subquery[projection_start : tile_subquery.index(" FROM budgeted")]
    return tuple(column.strip() for column in projection.split(","))


class ThresholdForecastStore(PsycopgForecastStore):
    def __init__(self) -> None:
        super().__init__("postgresql://test")

    def _transaction(self) -> Any:
        return _NullTransaction()

    def _validate_series_target(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
    ) -> None:
        assert (basin_version_id, segment_id, river_network_version_id) == ("basin_v1", "seg_002", "rnv_v1")
        del cursor

    def _per_source_latest_cycles(self, cursor: Any, **_kwargs: Any) -> dict[str, datetime]:
        del cursor
        return {"forecast_gfs_deterministic": VALID_TIME_1}

    def _fetch_forecast_segment_rows(self, cursor: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        del cursor
        return [
            {
                "scenario_id": "forecast_gfs_deterministic",
                "model_id": "model_1",
                "river_network_version_id": "rnv_v1",
                "source_id": "GFS",
                "cycle_time": VALID_TIME_1,
                "valid_time": VALID_TIME_1,
                "value": 2200.0,
                "unit": "m3/s",
            }
        ]

    def _fetch_frequency_thresholds(
        self,
        cursor: Any,
        *,
        model_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any] | None:
        del cursor
        assert (model_id, river_network_version_id, segment_id) == ("model_1", "rnv_v1", "seg_002")
        return {"Q2": 1200.0, "Q5": 1800.0, "Q10": 2300.0, "Q20": 2900.0, "Q50": 3700.0, "Q100": 4500.0}


class _NullTransaction:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_args: Any) -> bool:
        return False


@contextmanager
def _client() -> Iterator[TestClient]:
    with _store() as session:
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                yield client
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)


@contextmanager
def _store() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_schemas(engine)
    with engine.begin() as connection:
        _create_tables(connection)
        _seed_data(connection)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _seed_mvt_cache_row(session: Session, tile: flood_alert_routes.TileInput, data: bytes, **overrides: Any) -> None:
    flood_alert_routes.build_raw_tile_response(session, tile, data)
    updates = []
    params: dict[str, Any] = {"cache_key": cache_key(tile)}
    for index, (column, value) in enumerate(overrides.items()):
        param_name = f"value_{index}"
        updates.append(f"{column} = :{param_name}")
        params[param_name] = value
    if not updates:
        return
    session.execute(
        text(f"UPDATE map.tile_cache SET {', '.join(updates)} WHERE cache_key = :cache_key"),
        params,
    )
    session.commit()


def _route_tile_source_version(session: Session, tile: flood_alert_routes.TileInput) -> str:
    if tile.layer_id == "river-network":
        return flood_alert_routes._river_network_source_version(session, tile.source_id)
    if tile.layer_id == "met-stations":
        return flood_alert_routes._station_source_version(session, tile.source_id)
    return flood_alert_routes._run_source_version(flood_alert_routes._require_run(session, tile.source_id))


def _with_route_source_version(session: Session, tile: flood_alert_routes.TileInput) -> flood_alert_routes.TileInput:
    return flood_alert_routes.TileInput(
        layer_id=tile.layer_id,
        source_id=tile.source_id,
        source_version=_route_tile_source_version(session, tile),
        valid_time=tile.valid_time,
        z=tile.z,
        x=tile.x,
        y=tile.y,
        style_id=tile.style_id,
        variant_id=tile.variant_id,
        schema_version=tile.schema_version,
        encoder_version=tile.encoder_version,
    )


class _StationMvtFakePostgresSession:
    def __init__(self, *, tile_row: dict[str, Any]) -> None:
        self.tile_row = tile_row

    def get_bind(self) -> Any:
        class FakeDialect:
            name = "postgresql"

        class FakeBind:
            dialect = FakeDialect()

        return FakeBind()

    def execute(self, statement: Any, parameters: dict[str, Any]) -> Any:
        sql = str(statement)
        if "FROM met.met_station" in sql and "ST_AsMVT" not in sql:
            assert parameters == {
                "basin_version_id": "basin_v1",
                "limit": flood_alert_routes.FLOOD_RETURN_PERIOD_MAP_MAX_LIMIT + 1,
            }
            normalized_sql = re.sub(r"\s+", " ", sql)
            assert "WHERE basin_version_id = :basin_version_id AND active_flag = true" in normalized_sql
            assert "LIMIT :limit" in normalized_sql
            return _FakeMvtResult(
                None,
                [
                    {
                        "station_id": "station_001",
                        "basin_version_id": "basin_v1",
                        "station_name": "Station 001",
                        "station_role": "forcing_proxy",
                        "active_flag": True,
                        "geom": "01010000208A1100000000000000805B400000000000003E40",
                        "created_at": VALID_TIME_1,
                    }
                ],
            )
        if "ST_TileEnvelope(:z, :x, :y)" in sql:
            assert parameters["basin_version_id"] == "basin_v1"
            assert (parameters["z"], parameters["x"], parameters["y"]) == (6, 12, 24)
            assert "AND ms.active_flag = true" in re.sub(r"\s+", " ", sql)
            return _FakeMvtResult(self.tile_row)
        if "information_schema.tables" in sql:
            return _FakeMvtResult(None)
        raise AssertionError(f"Unexpected SQL in station MVT fake session: {sql}")

    def rollback(self) -> None:
        return None


class _FakeMvtResult:
    def __init__(self, row: dict[str, Any] | None, rows: list[dict[str, Any]] | None = None) -> None:
        self.row = row
        self.rows = rows if rows is not None else ([row] if row is not None else [])

    def mappings(self) -> _FakeMvtResult:
        return self

    def first(self) -> dict[str, Any] | None:
        return self.row

    def all(self) -> list[dict[str, Any]]:
        return self.rows


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS flood")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS map")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")


def _create_tables(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE core.basin_version (
                basin_version_id TEXT PRIMARY KEY,
                basin_id TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE core.model_instance (
                model_id TEXT PRIMARY KEY,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE core.river_network_version (
                river_network_version_id TEXT PRIMARY KEY,
                basin_version_id TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE core.river_segment (
                river_segment_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                geom TEXT,
                properties_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (river_segment_id, river_network_version_id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.hydro_run (
                run_id TEXT PRIMARY KEY,
                run_type TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                source_id TEXT,
                cycle_time DATETIME,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status TEXT NOT NULL,
                run_manifest_uri TEXT NOT NULL,
                updated_at DATETIME
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.river_timeseries (
                run_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                variable TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                quality_flag TEXT DEFAULT 'ok'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.return_period_result (
                run_id TEXT NOT NULL,
                scenario_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                duration TEXT NOT NULL,
                q_value REAL NOT NULL,
                q_unit TEXT NOT NULL DEFAULT 'm3/s',
                return_period REAL,
                warning_level TEXT,
                source_id TEXT,
                cycle_time DATETIME,
                max_over_window BOOLEAN NOT NULL DEFAULT 0,
                quality_flag TEXT NOT NULL DEFAULT 'ok',
                PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time, max_over_window)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.run_product_quality (
                run_id TEXT PRIMARY KEY,
                result_rows INTEGER NOT NULL DEFAULT 0,
                max_result_rows INTEGER NOT NULL DEFAULT 0,
                return_period_rows INTEGER NOT NULL DEFAULT 0,
                warning_rows INTEGER NOT NULL DEFAULT 0,
                max_return_period_rows INTEGER NOT NULL DEFAULT 0,
                max_warning_rows INTEGER NOT NULL DEFAULT 0,
                quality_state TEXT NOT NULL DEFAULT 'ready',
                quality_source TEXT NOT NULL DEFAULT 'historical_backfill',
                unavailable_products TEXT NOT NULL DEFAULT '[]',
                residual_blockers TEXT NOT NULL DEFAULT '[]',
                expected_result_rows INTEGER NOT NULL DEFAULT 0,
                expected_max_result_rows INTEGER NOT NULL DEFAULT 0,
                expected_timestep_result_rows INTEGER NOT NULL DEFAULT 0,
                meaningful_result_rows INTEGER NOT NULL DEFAULT 0,
                meaningful_max_result_rows INTEGER NOT NULL DEFAULT 0,
                meaningful_timestep_result_rows INTEGER NOT NULL DEFAULT 0,
                no_frequency_curve_rows INTEGER NOT NULL DEFAULT 0,
                no_usable_frequency_curve_rows INTEGER NOT NULL DEFAULT 0,
                warning_threshold_unavailable_rows INTEGER NOT NULL DEFAULT 0,
                refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE flood.flood_frequency_curve (
                curve_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                duration TEXT NOT NULL,
                method TEXT NOT NULL,
                sample_period_start DATE NOT NULL,
                sample_period_end DATE NOT NULL,
                sample_size INTEGER NOT NULL,
                parameters_json TEXT NOT NULL,
                q2 REAL,
                q5 REAL,
                q10 REAL,
                q20 REAL,
                q50 REAL,
                q100 REAL,
                unit TEXT NOT NULL,
                quality_flag TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE map.tile_layer (
                layer_id TEXT PRIMARY KEY,
                layer_type TEXT NOT NULL,
                source_run_id TEXT,
                source_product_id TEXT,
                source_version TEXT,
                variable TEXT,
                valid_time DATETIME,
                tile_format TEXT NOT NULL,
                tile_uri_template TEXT NOT NULL,
                maplibre_source_layer TEXT,
                property_schema_version TEXT,
                cache_version TEXT,
                fallback_available BOOLEAN NOT NULL DEFAULT 0,
                release_blocking BOOLEAN NOT NULL DEFAULT 0,
                min_zoom INTEGER NOT NULL DEFAULT 0,
                max_zoom INTEGER NOT NULL DEFAULT 14,
                published_flag BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE map.tile_cache (
                layer_id TEXT NOT NULL REFERENCES tile_layer(layer_id),
                z INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                tile_data BLOB,
                tile_uri TEXT,
                cache_key TEXT,
                etag TEXT,
                checksum TEXT,
                source_id TEXT,
                source_version TEXT,
                valid_time DATETIME,
                style_id TEXT NOT NULL DEFAULT 'default',
                schema_version TEXT,
                encoder_version TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (cache_key)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE met.met_station (
                station_id TEXT PRIMARY KEY,
                basin_version_id TEXT NOT NULL,
                station_name TEXT,
                geom TEXT NOT NULL,
                elevation_m REAL,
                station_role TEXT NOT NULL DEFAULT 'forcing_proxy',
                active_flag BOOLEAN NOT NULL DEFAULT 1,
                properties_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )


def _seed_data(connection: Any) -> None:
    connection.execute(
        text("INSERT INTO core.basin_version (basin_version_id, basin_id) VALUES ('basin_v1', 'yangtze')")
    )
    connection.execute(
        text("INSERT INTO core.basin_version (basin_version_id, basin_id) VALUES ('basin_v2', 'pearl')")
    )
    connection.execute(
        text(
            """
            INSERT INTO core.river_network_version (river_network_version_id, basin_version_id)
            VALUES ('rnv_v1', 'basin_v1'), ('rnv_v2', 'basin_v2')
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id)
            VALUES ('model_1', 'basin_v1', 'rnv_v1')
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO met.met_station (
                station_id, basin_version_id, station_name, geom, elevation_m,
                station_role, active_flag, properties_json, created_at
            )
            VALUES
                (
                    'station_001',
                    'basin_v1',
                    'Station 001',
                    'POINT(110 30)',
                    100.0,
                    'forcing_proxy',
                    1,
                    '{}',
                    :created_at
                ),
                (
                    'station_002',
                    'basin_v1',
                    'Station 002',
                    'POINT(111 31)',
                    120.0,
                    'forcing_grid',
                    1,
                    '{}',
                    :created_at
                ),
                (
                    'station_003',
                    'basin_v2',
                    'Station 003',
                    'POINT(112 32)',
                    80.0,
                    'forcing_proxy',
                    1,
                    '{}',
                    :created_at
                )
            """
        ),
        {"created_at": datetime(2026, 5, 3, tzinfo=UTC)},
    )
    for segment_id, rnv, lon, lat, name in [
        ("seg_001", "rnv_v1", 110.0, 30.0, "Segment 1"),
        ("seg_002", "rnv_v1", 111.0, 31.0, "Segment 2"),
        ("seg_003", "rnv_v1", 112.0, 32.0, "Segment 3"),
        ("seg_004", "rnv_v2", 113.0, 33.0, "Segment 4"),
        ("seg_no_curve", "rnv_v1", 114.0, 34.0, "No Curve"),
        ("dup_seg", "rnv_v1", 115.0, 35.0, "Duplicate Segment V1"),
        ("dup_seg", "rnv_v2", 116.0, 36.0, "Duplicate Segment V2"),
    ]:
        connection.execute(
            text(
                """
                INSERT INTO core.river_segment (
                    river_segment_id, river_network_version_id, geom, properties_json
                )
                VALUES (:segment_id, :rnv, :geom, :properties)
                """
            ),
            {
                "segment_id": segment_id,
                "rnv": rnv,
                "geom": f'{{"type":"LineString","coordinates":[[{lon},{lat}],[{lon + 0.5},{lat + 0.5}]]}}',
                "properties": f'{{"name":"{name}"}}',
            },
        )
    for run_id, status in [
        (RUN_ID, "frequency_done"),
        (PUBLISHED_RUN_ID, "published"),
        (DUPLICATE_SEGMENT_RUN_ID, "frequency_done"),
        (DUPLICATE_NETWORK_TIE_RUN_ID, "frequency_done"),
        (TIMESTEP_DUPLICATE_RUN_ID, "frequency_done"),
        ("run_oversized_geometry", "frequency_done"),
        (RECOMPUTE_MOVED_PEAK_RUN_ID, "parsed"),
        ("run_pending", "parsed"),
        ("run_empty", "frequency_done"),
        ("run_stray", "parsed"),
        ("run_warning_unavailable", "frequency_done"),
        (PARTIAL_ROUTE_RUN_ID, "frequency_done"),
        (PARTIAL_ROUTE_WARNING_RUN_ID, "frequency_done"),
    ]:
        connection.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri, updated_at
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, :status, 'object://manifest', :updated_at
                )
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
                "start_time": datetime(2026, 5, 3, tzinfo=UTC),
                "end_time": datetime(2026, 5, 10, tzinfo=UTC),
                "updated_at": datetime(2026, 5, 3, 1, tzinfo=UTC),
            },
        )
    _insert_result(connection, "seg_001", "basin_v1", "rnv_v1", VALID_TIME_2, 100.0, 1.5, "normal", True)
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2 + timedelta(hours=1),
        250.0,
        12.0,
        "watch",
        True,
    )
    _insert_result(connection, "seg_003", "basin_v1", "rnv_v1", VALID_TIME_2, 350.0, 55.0, "severe", True)
    _insert_result(connection, "seg_004", "basin_v2", "rnv_v2", VALID_TIME_2, 300.0, 25.0, "high_risk", True)
    _insert_result(connection, "seg_001", "basin_v1", "rnv_v1", VALID_TIME_1, 110.0, 3.0, "elevated", False)
    _insert_result(connection, "seg_002", "basin_v1", "rnv_v1", VALID_TIME_1, 210.0, 4.0, "elevated", False)
    _insert_result(connection, "seg_002", "basin_v1", "rnv_v1", VALID_TIME_2, 260.0, 22.0, "warning", False)
    _insert_timeseries_result(connection, "seg_001", RUN_ID, VALID_TIME_1, 110.0)
    _insert_timeseries_result(connection, "seg_002", RUN_ID, VALID_TIME_1, 210.0)
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        123.0,
        6.0,
        "watch",
        False,
        run_id=TIMESTEP_DUPLICATE_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        987.0,
        90.0,
        "severe",
        True,
        run_id=TIMESTEP_DUPLICATE_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_no_curve",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2 + timedelta(hours=2),
        90.0,
        None,
        None,
        False,
        quality_flag="no_usable_frequency_curve",
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        100.0,
        1.5,
        "normal",
        True,
        run_id=PARTIAL_ROUTE_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        200.0,
        4.0,
        "watch",
        True,
        run_id=PARTIAL_ROUTE_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        110.0,
        3.0,
        "elevated",
        False,
        run_id=PARTIAL_ROUTE_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        210.0,
        None,
        None,
        False,
        run_id=PARTIAL_ROUTE_RUN_ID,
        quality_flag="no_usable_frequency_curve",
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        100.0,
        1.5,
        "normal",
        True,
        run_id=PARTIAL_ROUTE_WARNING_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        200.0,
        4.0,
        "watch",
        True,
        run_id=PARTIAL_ROUTE_WARNING_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        110.0,
        3.0,
        "elevated",
        False,
        run_id=PARTIAL_ROUTE_WARNING_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        210.0,
        4.0,
        None,
        False,
        run_id=PARTIAL_ROUTE_WARNING_RUN_ID,
        quality_flag="warning_thresholds_unavailable",
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        100.0,
        1.5,
        "normal",
        True,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2 + timedelta(hours=1),
        250.0,
        12.0,
        "watch",
        True,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_003",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        350.0,
        55.0,
        "severe",
        True,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_004",
        "basin_v2",
        "rnv_v2",
        VALID_TIME_2,
        300.0,
        25.0,
        "high_risk",
        True,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        110.0,
        3.0,
        "elevated",
        False,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        210.0,
        4.0,
        "elevated",
        False,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        260.0,
        22.0,
        "warning",
        False,
        run_id=PUBLISHED_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_003",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_2,
        350.0,
        55.0,
        "severe",
        True,
        run_id="run_stray",
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        95.0,
        None,
        None,
        True,
        run_id="run_empty",
        quality_flag="no_usable_frequency_curve",
    )
    _insert_result(
        connection,
        "dup_seg",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        111.0,
        7.0,
        "watch",
        True,
        run_id=DUPLICATE_SEGMENT_RUN_ID,
    )
    _insert_result(
        connection,
        "dup_seg",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        110.0,
        6.0,
        "watch",
        False,
        run_id=DUPLICATE_SEGMENT_RUN_ID,
    )
    _insert_result(
        connection,
        "dup_seg",
        "basin_v2",
        "rnv_v2",
        VALID_TIME_1,
        222.0,
        80.0,
        "severe",
        True,
        run_id=DUPLICATE_SEGMENT_RUN_ID,
    )
    _insert_result(
        connection,
        "dup_seg",
        "basin_v2",
        "rnv_v2",
        VALID_TIME_1,
        220.0,
        70.0,
        "severe",
        False,
        run_id=DUPLICATE_SEGMENT_RUN_ID,
    )
    _insert_result(
        connection,
        "dup_seg",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        500.0,
        10.0,
        "watch",
        True,
        run_id=DUPLICATE_NETWORK_TIE_RUN_ID,
    )
    _insert_result(
        connection,
        "dup_seg",
        "basin_v2",
        "rnv_v2",
        VALID_TIME_1,
        500.0,
        10.0,
        "watch",
        True,
        run_id=DUPLICATE_NETWORK_TIE_RUN_ID,
    )
    _insert_result(
        connection,
        "seg_002",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        105.0,
        None,
        None,
        True,
        run_id="run_empty",
        quality_flag="no_usable_frequency_curve",
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        180.0,
        8.0,
        None,
        True,
        run_id="run_warning_unavailable",
        quality_flag="warning_thresholds_unavailable",
    )
    _insert_result(
        connection,
        "seg_001",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        180.0,
        8.0,
        None,
        False,
        run_id="run_warning_unavailable",
        quality_flag="warning_thresholds_unavailable",
    )
    _insert_timeseries_result(connection, "seg_001", RECOMPUTE_MOVED_PEAK_RUN_ID, VALID_TIME_1, 110.0)
    _insert_timeseries_result(connection, "seg_001", RECOMPUTE_MOVED_PEAK_RUN_ID, VALID_TIME_2, 260.0)
    _insert_timeseries_result(connection, "seg_002", RECOMPUTE_MOVED_PEAK_RUN_ID, VALID_TIME_1, 210.0)
    _insert_timeseries_result(connection, "seg_002", RECOMPUTE_MOVED_PEAK_RUN_ID, VALID_TIME_2, 150.0)
    _insert_oversized_geometry_case(connection)
    connection.execute(
        text(
            """
            INSERT INTO flood.flood_frequency_curve (
                curve_id, model_id, river_network_version_id, basin_version_id, river_segment_id,
                duration, method, sample_period_start, sample_period_end, sample_size, parameters_json,
                q2, q5, q10, q20, q50, q100, unit, quality_flag
            )
            VALUES (
                'curve_seg_002', 'model_1', 'rnv_v1', 'basin_v1', 'seg_002',
                '1h', 'P-III', '1980-01-01', '2019-12-31', 40,
                '{"sample_quality":{"Q20":{"quality_flag":"ok"}}}',
                1200, 1800, 2300, 2900, 3700, 4500, 'm3/s', 'ok'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO flood.flood_frequency_curve (
                curve_id, model_id, river_network_version_id, basin_version_id, river_segment_id,
                duration, method, sample_period_start, sample_period_end, sample_size, parameters_json,
                q2, q5, q10, q20, q50, q100, unit, quality_flag
            )
            VALUES (
                'curve_seg_001', 'model_1', 'rnv_v1', 'basin_v1', 'seg_001',
                '1h', 'P-III', '1980-01-01', '2019-12-31', 40,
                '{"sample_quality":{"Q20":{"quality_flag":"ok"},"Q50":{"quality_flag":"ok"},"Q100":{"quality_flag":"ok"}}}',
                100, 150, 200, 250, 350, 400, 'm3/s', 'ok'
            )
            """
        )
    )


def _insert_oversized_geometry_case(connection: Any) -> None:
    coordinates = ",".join(f"[{110 + index * 0.0001:.4f},{30 + index * 0.0001:.4f}]" for index in range(10_001))
    connection.execute(
        text(
            """
            INSERT INTO core.river_segment (
                river_segment_id, river_network_version_id, geom, properties_json
            )
            VALUES ('seg_oversized', 'rnv_v1', :geom, '{"name":"Oversized"}')
            """
        ),
        {"geom": f'{{"type":"LineString","coordinates":[{coordinates}]}}'},
    )
    _insert_result(
        connection,
        "seg_oversized",
        "basin_v1",
        "rnv_v1",
        VALID_TIME_1,
        999.0,
        100.0,
        "severe",
        False,
        run_id="run_oversized_geometry",
    )


def _insert_result(
    connection: Any,
    segment_id: str,
    basin_version_id: str,
    rnv: str,
    valid_time: datetime,
    q_value: float,
    return_period: float | None,
    warning_level: str | None,
    max_over_window: bool,
    *,
    run_id: str = RUN_ID,
    quality_flag: str = "ok",
    duration: str = "1h",
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO flood.return_period_result (
                run_id, scenario_id, basin_version_id, river_network_version_id, model_id,
                river_segment_id, valid_time, duration, q_value, q_unit, return_period,
                warning_level, source_id, cycle_time, max_over_window, quality_flag
            )
            VALUES (
                :run_id, 'forecast_gfs_deterministic', :basin_version_id, :rnv, 'model_1',
                :segment_id, :valid_time, :duration, :q_value, 'm3/s', :return_period,
                :warning_level, 'GFS', :cycle_time, :max_over_window, :quality_flag
            )
            """
        ),
        {
            "run_id": run_id,
            "basin_version_id": basin_version_id,
            "rnv": rnv,
            "segment_id": segment_id,
            "valid_time": valid_time,
            "q_value": q_value,
            "return_period": return_period,
            "warning_level": warning_level,
            "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
            "max_over_window": max_over_window,
            "quality_flag": quality_flag,
            "duration": duration,
        },
    )
    _refresh_run_quality(connection, run_id)


def _insert_hydro_run(
    connection: Any,
    *,
    run_id: str,
    cycle_time: datetime,
    status: str = "frequency_done",
    model_id: str = "model_1",
    basin_version_id: str = "basin_v1",
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                start_time, end_time, status, run_manifest_uri, updated_at
            )
            VALUES (
                :run_id, 'forecast', 'forecast_gfs_deterministic', :model_id, :basin_version_id,
                'GFS', :cycle_time, :cycle_time, :end_time, :status, 'object://manifest', :updated_at
            )
            """
        ),
        {
            "run_id": run_id,
            "model_id": model_id,
            "basin_version_id": basin_version_id,
            "cycle_time": cycle_time,
            "end_time": cycle_time + timedelta(days=7),
            "status": status,
            "updated_at": cycle_time + timedelta(hours=1),
        },
    )


def _write_explicit_flood_quality(
    connection: Any,
    *,
    run_id: str,
    quality_state: str,
    unavailable_products: list[str],
    residual_blockers: list[dict[str, Any]],
    expected_result_rows: int,
    meaningful_result_rows: int,
    no_frequency_curve_rows: int,
    no_usable_frequency_curve_rows: int,
    expected_max_result_rows: int = 0,
    expected_timestep_result_rows: int = 0,
    meaningful_max_result_rows: int = 0,
    meaningful_timestep_result_rows: int = 0,
    warning_threshold_unavailable_rows: int = 0,
    result_rows: int = 0,
    max_result_rows: int = 0,
    return_period_rows: int = 0,
    warning_rows: int = 0,
    max_return_period_rows: int = 0,
    max_warning_rows: int = 0,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO flood.run_product_quality (
                run_id, result_rows, max_result_rows, return_period_rows, warning_rows,
                max_return_period_rows, max_warning_rows, quality_state, quality_source,
                unavailable_products, residual_blockers, expected_result_rows, expected_max_result_rows,
                expected_timestep_result_rows, meaningful_result_rows, meaningful_max_result_rows,
                meaningful_timestep_result_rows, no_frequency_curve_rows, no_usable_frequency_curve_rows,
                warning_threshold_unavailable_rows, refreshed_at
            )
            VALUES (
                :run_id, :result_rows, :max_result_rows, :return_period_rows, :warning_rows,
                :max_return_period_rows, :max_warning_rows, :quality_state, 'explicit',
                :unavailable_products, :residual_blockers, :expected_result_rows, :expected_max_result_rows,
                :expected_timestep_result_rows, :meaningful_result_rows, :meaningful_max_result_rows,
                :meaningful_timestep_result_rows, :no_frequency_curve_rows, :no_usable_frequency_curve_rows,
                :warning_threshold_unavailable_rows, CURRENT_TIMESTAMP
            )
            ON CONFLICT (run_id) DO UPDATE SET
                result_rows = excluded.result_rows,
                max_result_rows = excluded.max_result_rows,
                return_period_rows = excluded.return_period_rows,
                warning_rows = excluded.warning_rows,
                max_return_period_rows = excluded.max_return_period_rows,
                max_warning_rows = excluded.max_warning_rows,
                quality_state = excluded.quality_state,
                quality_source = excluded.quality_source,
                unavailable_products = excluded.unavailable_products,
                residual_blockers = excluded.residual_blockers,
                expected_result_rows = excluded.expected_result_rows,
                expected_max_result_rows = excluded.expected_max_result_rows,
                expected_timestep_result_rows = excluded.expected_timestep_result_rows,
                meaningful_result_rows = excluded.meaningful_result_rows,
                meaningful_max_result_rows = excluded.meaningful_max_result_rows,
                meaningful_timestep_result_rows = excluded.meaningful_timestep_result_rows,
                no_frequency_curve_rows = excluded.no_frequency_curve_rows,
                no_usable_frequency_curve_rows = excluded.no_usable_frequency_curve_rows,
                warning_threshold_unavailable_rows = excluded.warning_threshold_unavailable_rows,
                refreshed_at = excluded.refreshed_at
            """
        ),
        {
            "run_id": run_id,
            "result_rows": result_rows,
            "max_result_rows": max_result_rows,
            "return_period_rows": return_period_rows,
            "warning_rows": warning_rows,
            "max_return_period_rows": max_return_period_rows,
            "max_warning_rows": max_warning_rows,
            "quality_state": quality_state,
            "unavailable_products": json.dumps(unavailable_products),
            "residual_blockers": json.dumps(residual_blockers),
            "expected_result_rows": expected_result_rows,
            "expected_max_result_rows": expected_max_result_rows,
            "expected_timestep_result_rows": expected_timestep_result_rows,
            "meaningful_result_rows": meaningful_result_rows,
            "meaningful_max_result_rows": meaningful_max_result_rows,
            "meaningful_timestep_result_rows": meaningful_timestep_result_rows,
            "no_frequency_curve_rows": no_frequency_curve_rows,
            "no_usable_frequency_curve_rows": no_usable_frequency_curve_rows,
            "warning_threshold_unavailable_rows": warning_threshold_unavailable_rows,
        },
    )


def _refresh_run_quality(connection: Any, run_id: str) -> None:
    row = connection.execute(
        text(
            """
            SELECT
                COUNT(*) AS result_rows,
                SUM(CASE WHEN max_over_window = 1 THEN 1 ELSE 0 END) AS max_result_rows,
                SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows,
                SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows,
                SUM(CASE WHEN max_over_window = 1 AND return_period IS NOT NULL THEN 1 ELSE 0 END)
                    AS max_return_period_rows,
                SUM(CASE WHEN max_over_window = 1 AND warning_level IS NOT NULL THEN 1 ELSE 0 END)
                    AS max_warning_rows,
                SUM(CASE WHEN quality_flag = 'no_frequency_curve' THEN 1 ELSE 0 END)
                    AS no_frequency_curve_rows,
                SUM(CASE WHEN quality_flag = 'no_usable_frequency_curve' THEN 1 ELSE 0 END)
                    AS no_usable_frequency_curve_rows,
                SUM(CASE WHEN quality_flag = 'warning_thresholds_unavailable' THEN 1 ELSE 0 END)
                    AS warning_threshold_unavailable_rows
            FROM flood.return_period_result
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id},
    ).mappings().one()
    if int(row["result_rows"] or 0) <= 0:
        connection.execute(
            text("DELETE FROM flood.run_product_quality WHERE run_id = :run_id"),
            {"run_id": run_id},
        )
        return
    result_rows = int(row["result_rows"] or 0)
    max_result_rows = int(row["max_result_rows"] or 0)
    return_period_rows = int(row["return_period_rows"] or 0)
    warning_rows = int(row["warning_rows"] or 0)
    max_return_period_rows = int(row["max_return_period_rows"] or 0)
    max_warning_rows = int(row["max_warning_rows"] or 0)
    no_frequency_curve_rows = int(row["no_frequency_curve_rows"] or 0)
    no_usable_frequency_curve_rows = int(row["no_usable_frequency_curve_rows"] or 0)
    warning_threshold_unavailable_rows = int(row["warning_threshold_unavailable_rows"] or 0)
    meaningful_result_rows = max(return_period_rows, warning_rows)
    meaningful_max_result_rows = max(max_return_period_rows, max_warning_rows)
    unavailable_products: list[str] = []
    residual_blockers: list[dict[str, Any]] = []
    if return_period_rows <= 0:
        unavailable_products.append("return_period_result")
        residual_blockers.append(
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": "missing_return_period_result",
                "run_id": run_id,
                "residual_risk": "No non-null return-period rows are available for this run.",
                "count": result_rows,
            }
        )
    if warning_threshold_unavailable_rows > 0:
        unavailable_products.append("warning_thresholds")
        residual_blockers.append(
            {
                "code": "WARNING_THRESHOLDS_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": "warning_thresholds_unavailable",
                "run_id": run_id,
                "residual_risk": "warning_level remains null for return-period rows.",
                "count": warning_threshold_unavailable_rows,
            }
        )
    unavailable_products = sorted(set(unavailable_products))
    if return_period_rows <= 0 or warning_rows < return_period_rows:
        quality_state = "unavailable"
    else:
        quality_state = "ready"
    connection.execute(
        text(
            """
            INSERT INTO flood.run_product_quality (
                run_id, result_rows, max_result_rows, return_period_rows, warning_rows,
                max_return_period_rows, max_warning_rows, quality_state, quality_source,
                unavailable_products, residual_blockers, expected_result_rows, expected_max_result_rows,
                expected_timestep_result_rows, meaningful_result_rows, meaningful_max_result_rows,
                meaningful_timestep_result_rows, no_frequency_curve_rows, no_usable_frequency_curve_rows,
                warning_threshold_unavailable_rows, refreshed_at
            )
            VALUES (
                :run_id, :result_rows, :max_result_rows, :return_period_rows, :warning_rows,
                :max_return_period_rows, :max_warning_rows, :quality_state, 'historical_backfill',
                :unavailable_products, :residual_blockers, :expected_result_rows, :expected_max_result_rows,
                :expected_timestep_result_rows, :meaningful_result_rows, :meaningful_max_result_rows,
                :meaningful_timestep_result_rows, :no_frequency_curve_rows, :no_usable_frequency_curve_rows,
                :warning_threshold_unavailable_rows, CURRENT_TIMESTAMP
            )
            ON CONFLICT (run_id) DO UPDATE SET
                result_rows = excluded.result_rows,
                max_result_rows = excluded.max_result_rows,
                return_period_rows = excluded.return_period_rows,
                warning_rows = excluded.warning_rows,
                max_return_period_rows = excluded.max_return_period_rows,
                max_warning_rows = excluded.max_warning_rows,
                quality_state = excluded.quality_state,
                quality_source = excluded.quality_source,
                unavailable_products = excluded.unavailable_products,
                residual_blockers = excluded.residual_blockers,
                expected_result_rows = excluded.expected_result_rows,
                expected_max_result_rows = excluded.expected_max_result_rows,
                expected_timestep_result_rows = excluded.expected_timestep_result_rows,
                meaningful_result_rows = excluded.meaningful_result_rows,
                meaningful_max_result_rows = excluded.meaningful_max_result_rows,
                meaningful_timestep_result_rows = excluded.meaningful_timestep_result_rows,
                no_frequency_curve_rows = excluded.no_frequency_curve_rows,
                no_usable_frequency_curve_rows = excluded.no_usable_frequency_curve_rows,
                warning_threshold_unavailable_rows = excluded.warning_threshold_unavailable_rows,
                refreshed_at = excluded.refreshed_at
            """
        ),
        {
            "run_id": run_id,
            "result_rows": result_rows,
            "max_result_rows": max_result_rows,
            "return_period_rows": return_period_rows,
            "warning_rows": warning_rows,
            "max_return_period_rows": max_return_period_rows,
            "max_warning_rows": max_warning_rows,
            "quality_state": quality_state,
            "unavailable_products": json.dumps(unavailable_products),
            "residual_blockers": json.dumps(residual_blockers),
            "expected_result_rows": result_rows,
            "expected_max_result_rows": max_result_rows,
            "expected_timestep_result_rows": max(result_rows - max_result_rows, 0),
            "meaningful_result_rows": meaningful_result_rows,
            "meaningful_max_result_rows": meaningful_max_result_rows,
            "meaningful_timestep_result_rows": max(meaningful_result_rows - meaningful_max_result_rows, 0),
            "no_frequency_curve_rows": no_frequency_curve_rows,
            "no_usable_frequency_curve_rows": no_usable_frequency_curve_rows,
            "warning_threshold_unavailable_rows": warning_threshold_unavailable_rows,
        },
    )


def _insert_timeseries_result(
    connection: Any,
    segment_id: str,
    run_id: str,
    valid_time: datetime,
    value: float,
    *,
    variable: str = "q_down",
    unit: str = "m3/s",
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO hydro.river_timeseries (
                run_id, basin_version_id, river_network_version_id, river_segment_id,
                valid_time, variable, value, unit
            )
            VALUES (
                :run_id, 'basin_v1', 'rnv_v1', :segment_id,
                :valid_time, :variable, :value, :unit
            )
            """
        ),
        {
            "run_id": run_id,
            "segment_id": segment_id,
            "valid_time": valid_time,
            "value": value,
            "variable": variable,
            "unit": unit,
        },
    )


def _level_count(data: dict[str, Any], level: str) -> int:
    return {item["level"]: item["count"] for item in data["levels"]}[level]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
