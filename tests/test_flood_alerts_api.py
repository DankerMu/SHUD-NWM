from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import flood_alerts as flood_alert_routes
from apps.api.routes.forecast import get_forecast_store
from packages.common.forecast_store import PsycopgForecastStore

RUN_ID = "fcst_gfs_2026050300_all"
PUBLISHED_RUN_ID = "fcst_gfs_2026050300_published"
DUPLICATE_SEGMENT_RUN_ID = "fcst_gfs_2026050300_duplicate_segments"
VALID_TIME_1 = datetime(2026, 5, 3, 6, tzinfo=UTC)
VALID_TIME_2 = datetime(2026, 5, 3, 12, tzinfo=UTC)


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
        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&limit=2&offset=1")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 4
        assert [item["river_segment_id"] for item in data["items"]] == ["seg_004", "seg_002"]
        assert [item["rank"] for item in data["items"]] == [2, 3]

        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&basin_id=yangtze")
        assert response.status_code == 200
        basin_data = response.json()["data"]
        assert basin_data["total"] == 3
        assert {item["basin_version_id"] for item in basin_data["items"]} == {"basin_v1"}

        response = client.get(f"/api/v1/flood-alerts/ranking?run_id={RUN_ID}&valid_time={_iso(VALID_TIME_1)}")
        assert response.status_code == 200
        valid_time_data = response.json()["data"]
        assert [item["river_segment_id"] for item in valid_time_data["items"]] == ["seg_002", "seg_001"]


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
    assert [point["q_value"] for point in rnv_v1["timesteps"]] == [111.0]
    assert [point["q_value"] for point in rnv_v2["timesteps"]] == [222.0]
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
                "segment_id",
                "basin_version_id",
                "river_network_version_id",
                "value",
                "unit",
                "quality_flag",
                "return_period",
                "warning_level",
            }
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


def test_flood_tile_legacy_pbf_route_redirects_to_geojson_endpoint() -> None:
    with _client() as client:
        response = client.get(
            f"/api/v1/tiles/flood-return-period/{RUN_ID}/1h/{_iso(VALID_TIME_1)}/6/12/24.pbf",
            follow_redirects=False,
        )
        assert response.status_code == 307
        assert response.headers["location"].startswith("/api/v1/tiles/flood-return-period?")


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


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS flood")


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
                max_over_window BOOLEAN DEFAULT 0,
                quality_flag TEXT NOT NULL DEFAULT 'ok',
                PRIMARY KEY (run_id, river_network_version_id, river_segment_id, duration, valid_time)
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
                :segment_id, :valid_time, '1h', :q_value, 'm3/s', :return_period,
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
        },
    )


def _level_count(data: dict[str, Any], level: str) -> int:
    return {item["level"]: item["count"] for item in data["levels"]}[level]


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
