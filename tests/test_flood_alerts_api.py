from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
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
from services.tiles.mvt import MVT_MAX_BYTES, MVT_VALID_TIME_SAMPLE_LIMIT, cache_key
from workers.flood_frequency.return_period import compute_return_periods

RUN_ID = "fcst_gfs_2026050300_all"
PUBLISHED_RUN_ID = "fcst_gfs_2026050300_published"
DUPLICATE_SEGMENT_RUN_ID = "fcst_gfs_2026050300_duplicate_segments"
DUPLICATE_NETWORK_TIE_RUN_ID = "fcst_gfs_2026050300_duplicate_network_tie"
TIMESTEP_DUPLICATE_RUN_ID = "fcst_gfs_2026050300_timestep_duplicates"
RECOMPUTE_MOVED_PEAK_RUN_ID = "fcst_gfs_2026050300_recompute_moved_peak"
VALID_TIME_1 = datetime(2026, 5, 3, 6, tzinfo=UTC)
VALID_TIME_2 = datetime(2026, 5, 3, 12, tzinfo=UTC)
VALID_TIME_1_ISO = VALID_TIME_1.isoformat().replace("+00:00", "Z")
VALID_TIME_2_ISO = VALID_TIME_2.isoformat().replace("+00:00", "Z")


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
        assert valid_time_data["total_segments"] == 3
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
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total_segments"] == 2
        assert data["usable_curves"] == 0
        assert data["quality_note"] == "No usable frequency curves available"
        assert all(level["count"] == 0 for level in data["levels"])


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
            "seg_no_curve",
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
            f"?run_id={RUN_ID}&duration=1h&valid_time={_iso(VALID_TIME_1)}&limit=2"
        )

    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "FLOOD_RETURN_PERIOD_FEATURE_LIMIT_EXCEEDED"
    assert body["error"]["details"] == {"limit": 2}


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
    body = response.json()
    assert body["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
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
                assert (parameters["z"], parameters["x"], parameters["y"]) == (6, 12, 24)
                return FakeRowResult({"tile": b"live-tile", "source_feature_count": 1})
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS tile test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
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

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-protobuf")
    assert response.content == b"live-tile"
    checksum = hashlib.sha256(b"live-tile").hexdigest()
    assert response.headers["x-tile-checksum"] == checksum
    assert response.headers["etag"] == f'W/"m16-{checksum}"'
    assert response.headers["x-tile-cache"] == "bypass"


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
            f"/api/v1/tiles/hydro/{RUN_ID}/water_level/{VALID_TIME_1_ISO}/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="water-level",
                source_id=RUN_ID,
                source_version="rnv_v1",
                valid_time=VALID_TIME_1_ISO,
                z=6,
                x=12,
                y=24,
                variant_id="variable:water_level",
            ),
            "_fetch_hydro_mvt_tile_bytes",
            [("water-level", "hydrological_output", "water_level")],
        ),
        (
            "/api/v1/tiles/river-network/basin_v1/6/12/24.pbf",
            flood_alert_routes.TileInput(
                layer_id="river-network",
                source_id="basin_v1",
                source_version="basin_v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
            [("river-network", "river_network", None)],
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
        seeded = flood_alert_routes.build_raw_tile_response(session, seed_tile, b"cached-live-tile")

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
                source_version="basin_v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "river-network",
        ),
    ],
)
def test_seeded_live_mvt_cache_hit_still_requires_live_postgis(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    seed_tile: flood_alert_routes.TileInput,
    expected_layer: str,
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: True)
        flood_alert_routes.build_raw_tile_response(session, seed_tile, b"cached-live-tile")
        monkeypatch.setattr(flood_alert_routes, "_mvt_live_postgis_enabled", lambda _session: False)
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
        app.dependency_overrides[flood_alert_routes.get_flood_alert_session] = lambda: session
        try:
            with TestClient(app) as client:
                response = client.get(path)
        finally:
            app.dependency_overrides.pop(flood_alert_routes.get_flood_alert_session, None)

    assert response.status_code == 424
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
    assert response.json()["error"]["details"]["layer_id"] == expected_layer


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
                source_version="basin_v1",
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
                source_version="basin_v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
            {"source_id": "wrong-source"},
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
        _seed_mvt_cache_row(session, seed_tile, b"invalid-cached-pbf", **invalid_overrides)
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
                source_version="basin_v1",
                valid_time=None,
                z=6,
                x=12,
                y=24,
            ),
            "_fetch_river_network_mvt_tile_bytes",
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


def test_live_mvt_zero_feature_tile_returns_pbf_and_cache_headers(monkeypatch: pytest.MonkeyPatch) -> None:
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
                assert parameters["variable"] == "q_down"
                return FakeRowResult(
                    {
                        "tile": b"",
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
            raise AssertionError(f"Unexpected SQL in live PostGIS zero-feature test: {sql}")

        def rollback(self) -> None:
            return None

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_run",
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

    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == flood_alert_routes.MVT_MEDIA_TYPE
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.headers["x-tile-layer-id"] == "discharge"
    assert response.headers["x-tile-cache"] == "bypass"
    assert response.headers["x-mvt-schema-version"] == flood_alert_routes.MVT_SCHEMA_VERSION
    assert response.content == b""


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
    monkeypatch.setattr(
        flood_alert_routes,
        "_require_run",
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
        hydro = client.get(f"/api/v1/tiles/hydro/{RECOMPUTE_MOVED_PEAK_RUN_ID}/q_down/{_iso(VALID_TIME_1)}/4/12/6.pbf")
        river = client.get("/api/v1/tiles/river-network/basin_v1/4/12/6.pbf")

    assert hydro.status_code == 424
    assert hydro.json()["error"]["code"] == "MVT_LIVE_POSTGIS_UNAVAILABLE"
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
                assert f"'{expected_layer.replace('-', '_')}'" in sql or expected_layer == "hydro"
                for key, value in expected_params.items():
                    assert parameters[key] == value
                return FakeRowResult({"tile": b"live-tile", "source_feature_count": 1})
            if "information_schema.tables" in sql:
                return FakeRowResult(None)
            raise AssertionError(f"Unexpected SQL in live PostGIS tile test: {sql}")

    monkeypatch.setenv("NHMS_ENABLE_LIVE_POSTGIS_MVT", "true")
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
        f"/api/v1/tiles/hydro/{RUN_ID}/velocity/{VALID_TIME_1_ISO}/6/12/24.pbf",
        f"/api/v1/tiles/flood-return-period/{RUN_ID}/2h/{VALID_TIME_1_ISO}/6/12/24.pbf",
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


@pytest.mark.parametrize(
    ("path", "fetch_name"),
    [
        (f"/api/v1/tiles/hydro/{RUN_ID}/q_down/{VALID_TIME_1_ISO}/6/12/24.pbf", "_fetch_hydro_mvt_tile_bytes"),
        (
            f"/api/v1/tiles/hydro/{RUN_ID}/water_level/{VALID_TIME_1_ISO}/6/12/24.pbf",
            "_fetch_hydro_mvt_tile_bytes",
        ),
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


def test_hydro_layer_metadata_declares_public_cache_identity_and_legacy_aliases() -> None:
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    layers = {layer["layer_id"]: layer for layer in response.json()["data"]}
    discharge = layers["discharge"]["metadata"]
    water_level = layers["water-level"]["metadata"]
    assert discharge["cache_layer_id"] == "discharge"
    assert discharge["route_variable"] == "q_down"
    assert discharge["legacy_layer_ids"] == ["hydro:q_down"]
    assert water_level["cache_layer_id"] == "water-level"
    assert water_level["route_variable"] == "water_level"
    assert water_level["legacy_layer_ids"] == ["hydro:water_level"]


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
            if placeholder not in source_refs and placeholder not in documented_route_constants
        ]
        assert missing == []

    river_network = next(layer["metadata"] for layer in response.json()["data"] if layer["layer_id"] == "river-network")
    assert river_network["source_refs"]["basin_version_id"] == "basin_v1"
    metadata = {layer["layer_id"]: layer["metadata"] for layer in response.json()["data"]}
    assert metadata["flood-return-period"]["source_refs"]["duration"] == "1h"
    assert metadata["warning-level"]["source_refs"]["duration"] == "1h"
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
            _insert_timeseries_result(session, "seg_001", run_id, valid_time, 1.25, variable="water_level", unit="m")
        session.commit()

        def catalog_for(run_id: str) -> dict[str, dict[str, Any]]:
            return {
                layer.layer_id: layer.metadata or {}
                for layer in flood_alert_routes._default_layer_catalog(
                    session,
                    run_id=run_id,
                    source_version="rnv_v1",
                    basin_version_id="basin_v1",
                )
            }

        old_catalog = catalog_for(old_run_id)
        new_catalog = catalog_for(new_run_id)

    for layer_id in ("discharge", "water-level", "flood-return-period", "warning-level"):
        old_metadata = old_catalog[layer_id]
        new_metadata = new_catalog[layer_id]
        assert old_metadata["source_refs"]["source_version"] == new_metadata["source_refs"]["source_version"]
        assert old_metadata["source_refs"]["run_id"] == old_run_id
        assert new_metadata["source_refs"]["run_id"] == new_run_id
        assert old_metadata["valid_times"] != new_metadata["valid_times"]
        assert old_metadata["cache_version"] != new_metadata["cache_version"]
        assert old_metadata["cache_etag"] != new_metadata["cache_etag"]


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
        "water-level": flood_alert_routes.TileInput(
            layer_id="water-level",
            source_id=RUN_ID,
            source_version="rnv_v1",
            valid_time=VALID_TIME_1_ISO,
            z=6,
            x=12,
            y=24,
            variant_id="variable:water_level",
        ),
        "river-network": flood_alert_routes.TileInput(
            layer_id="river-network",
            source_id="basin_v1",
            source_version="basin_v1",
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
            receipt = flood_alert_routes.build_raw_tile_response(session, tile_input, f"{layer_id}-tile".encode())
            metadata = layers[layer_id]
            expected_layer_id = metadata["cache_layer_id"]
            assert receipt.layer_id == expected_layer_id
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
    assert "source_stats AS" in statement
    assert "FROM source_stats, budget_stats, prefilter_stats" in statement


def test_mvt_postgis_sql_shape_simplifies_all_production_layers_before_encoding() -> None:
    for layer in ("flood-return-period", "hydro", "river-network"):
        statement = flood_alert_routes.postgis_tile_sql(layer)
        assert "eligible AS" in statement
        assert "simplified AS" in statement
        assert "ST_SimplifyPreserveTopology" in statement
        assert "ST_MakeValid(ST_Transform(eligible.geom, 3857))" in statement
        assert "simplified.geom_3857" in statement
        assert statement.index("eligible AS") < statement.index("simplified AS") < statement.index("clipped AS")
        assert ":simplification_tolerance_m" in statement


def test_mvt_postgis_sql_shape_filters_over_budget_features_before_expensive_geometry_work() -> None:
    for layer in ("flood-return-period", "hydro", "river-network"):
        statement = flood_alert_routes.postgis_tile_sql(layer)
        eligible_index = statement.index("eligible AS")
        expensive_indexes = [
            statement.index("ST_MakeValid"),
            statement.index("ST_Transform(eligible.geom, 3857)"),
            statement.index("ST_SimplifyPreserveTopology"),
            statement.index("ST_AsMVTGeom"),
        ]
        assert "WHERE source_coordinate_count <= :feature_coordinate_limit" in statement
        assert "AND source_coordinate_dimensions <= :max_coordinate_dimensions" in statement
        assert all(eligible_index < expensive_index for expensive_index in expensive_indexes)
        assert statement.index("prefilter_stats AS") < statement.index("clipped AS")


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


def test_mvt_postgis_sql_projects_source_time_identity_through_public_allowlist() -> None:
    hydro = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("hydro"))
    flood = re.sub(r"\s+", " ", flood_alert_routes.postgis_tile_sql("flood-return-period"))

    hydro_source_cte = hydro[hydro.index("source_rows AS") : hydro.index("source_stats AS")]
    hydro_tile_projection = _mvt_tile_projection(hydro)
    assert "ts.run_id" in hydro_source_cte
    assert "ts.variable" in hydro_source_cte
    assert "to_char(ts.valid_time AT TIME ZONE 'UTC'" in hydro_source_cte
    assert "valid_time" in hydro_tile_projection
    assert "run_id" in hydro_tile_projection
    assert "variable" in hydro_tile_projection
    assert hydro_tile_projection.index("run_id") < hydro_tile_projection.index("valid_time")

    flood_source_cte = flood[flood.index("source_rows AS") : flood.index("source_stats AS")]
    flood_tile_projection = _mvt_tile_projection(flood)
    assert "r.run_id" in flood_source_cte
    assert "r.duration" in flood_source_cte
    assert "to_char(r.valid_time AT TIME ZONE 'UTC'" in flood_source_cte
    assert "valid_time" in flood_tile_projection
    assert "run_id" in flood_tile_projection
    assert "duration" in flood_tile_projection
    assert flood_tile_projection.index("run_id") < flood_tile_projection.index("valid_time")


def test_layer_metadata_property_schema_declares_public_source_time_identity() -> None:
    with _client() as client:
        response = client.get("/api/v1/layers")

    assert response.status_code == 200
    metadata = {layer["layer_id"]: layer["metadata"] for layer in response.json()["data"]}

    for layer_id in ("discharge", "water-level"):
        required = metadata[layer_id]["property_schema"]["required"]
        assert {"run_id", "variable", "valid_time"}.issubset(required)
        assert "duration" not in required

    for layer_id in ("flood-return-period", "warning-level"):
        required = metadata[layer_id]["property_schema"]["required"]
        assert {"run_id", "duration", "valid_time"}.issubset(required)
        assert "variable" not in required


def test_river_network_mvt_sql_scopes_basin_without_model_instance_cardinality_multiply() -> None:
    statement = flood_alert_routes.postgis_tile_sql("river-network")
    sql = re.sub(r"\s+", " ", statement)

    source_cte = sql[sql.index("source_rows AS") : sql.index("source_stats AS")]
    assert "WHERE EXISTS ( SELECT 1 FROM core.model_instance mi" in source_cte
    assert "mi.river_network_version_id = rs.river_network_version_id" in source_cte
    assert "mi.basin_version_id = :basin_version_id" in source_cte
    assert "JOIN core.model_instance" not in source_cte
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


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS flood")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS map")


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
                run_manifest_uri TEXT NOT NULL
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
            INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id)
            VALUES ('model_1', 'basin_v1', 'rnv_v1')
            """
        )
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
    ]:
        connection.execute(
            text(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, source_id, cycle_time,
                    start_time, end_time, status, run_manifest_uri
                )
                VALUES (
                    :run_id, 'forecast', 'forecast_gfs_deterministic', 'model_1', 'basin_v1',
                    'GFS', :cycle_time, :start_time, :end_time, :status, 'object://manifest'
                )
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "cycle_time": datetime(2026, 5, 3, tzinfo=UTC),
                "start_time": datetime(2026, 5, 3, tzinfo=UTC),
                "end_time": datetime(2026, 5, 10, tzinfo=UTC),
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
        VALID_TIME_1,
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
