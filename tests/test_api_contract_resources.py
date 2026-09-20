"""Data-source, met-station, model, basin and river-segment API contract partition.

The #2074 partition of the former 2,132-line `tests/test_api_contract.py` that
owns the resource-route contracts served out of the model registry and the
forcing/forecast stores: data sources and their cycles, met stations and station
series, model active/lifecycle/list/detail, basin version listings, river-segment
GeoJSON budgets and the forecast series query.

The shared doubles and assertion helpers live in `tests/api_contract_helpers.py`;
the published-document and generated-type cases stayed on
`tests/test_api_contract.py`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes.data_sources import get_data_source_store, get_station_lookup
from apps.api.routes.forecast import get_forecast_store
from apps.api.routes.models import get_model_registry_store
from packages.common.forecast_store import _station_variable_filter_tokens
from tests.api_contract_helpers import (
    STATION_SERIES_MISSING_REQUIRED_FILTER_MESSAGE,
    _assert_success_envelope,
    _DataSourceStore,
    _ForecastSeriesStore,
    _ModelRegistryStore,
    _OversizedRiverSegmentStore,
)


def test_data_sources_contract_uses_success_envelope() -> None:
    app.dependency_overrides[get_data_source_store] = lambda: _DataSourceStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/data-sources", params={"limit": 5, "offset": 0})
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data == {
        "items": [{"source_id": "GFS", "provider": "NOAA/NCEP", "format": "GRIB2"}],
        "total_count": 1,
        "limit": 5,
        "offset": 0,
    }


def test_data_source_cycles_contract_uses_success_envelope() -> None:
    app.dependency_overrides[get_data_source_store] = lambda: _DataSourceStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/data-sources/GFS/cycles", params={"limit": 5, "offset": 0})
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["items"] == [{"cycle_id": "GFS_2026051400", "source_id": "GFS", "status": "raw_complete"}]
    assert data["limit"] == 5
    assert data["offset"] == 0


def test_met_stations_contract_uses_success_envelope() -> None:
    app.dependency_overrides[get_data_source_store] = lambda: _DataSourceStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/met/stations", params={"basin_version_id": "basin_v1"})
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data["items"] == [{"station_id": "station_1", "basin_version_id": "basin_v1", "active_flag": True}]


def test_met_stations_repeated_variables_query_binds_all_values() -> None:
    class _RecordingMetStationStore:
        """Captures the variables argument delivered to list_met_stations."""

        def __init__(self) -> None:
            self.variables_calls: list[Any] = []

        def list_met_stations(self, **kwargs: Any) -> dict[str, Any]:
            self.variables_calls.append(kwargs.get("variables"))
            # Exercise the real coverage-token parser to confirm both variables bind.
            tokens = _station_variable_filter_tokens(kwargs.get("variables"))
            return {
                "items": [],
                "total_count": 0,
                "limit": kwargs["limit"],
                "offset": kwargs["offset"],
                "filters": {"variables": tokens},
            }

    store = _RecordingMetStationStore()
    app.dependency_overrides[get_data_source_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/met/stations",
                params=[("variables", "PRCP"), ("variables", "TEMP")],
            )
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)

    assert response.status_code == 200
    # Repeated query params must arrive as a list, not collapse to the first value.
    assert store.variables_calls == [["PRCP", "TEMP"]]
    data = _assert_success_envelope(response.json())
    assert data["filters"]["variables"] == ["PRCP", "TEMP"]


def test_met_station_series_forcing_version_alone_uses_error_envelope_without_store_delegation() -> None:
    store = _DataSourceStore()
    app.dependency_overrides[get_data_source_store] = lambda: store
    app.dependency_overrides[get_station_lookup] = object
    app.state.object_store_root = tempfile.mkdtemp(prefix="nhms-station-series-object-store-")
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/met/stations/station_1/series",
                params={"forcing_version_id": "forc_qhh_gfs_2026050700"},
            )
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)
        app.dependency_overrides.pop(get_station_lookup, None)

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["request_id"]
    assert body["error"]["code"] == "MISSING_REQUIRED_FILTER"
    assert body["error"]["message"] == STATION_SERIES_MISSING_REQUIRED_FILTER_MESSAGE
    assert body["error"]["details"] == {
        "required_alternatives": [
            ["forcing_version_id"],
            ["model_id", "source_id", "cycle_time"],
        ]
    }
    assert store.station_series_calls == []


def test_model_active_contract_accepts_active_and_active_flag() -> None:
    store = _ModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            active_response = client.put(
                "/api/v1/models/model_1/active",
                json={"active": True},
                headers={"X-User-Role": "model_admin"},
            )
            active_flag_response = client.put(
                "/api/v1/models/model_1/active",
                json={"active_flag": False},
                headers={"X-User-Role": "model_admin"},
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert active_response.status_code == 200
    assert active_response.json()["status"] == "ok"
    assert active_response.json()["data"]["status"] == "allowed"
    assert active_response.json()["data"]["model"]["active_flag"] is True
    assert active_flag_response.status_code == 200
    assert active_flag_response.json()["status"] == "ok"
    assert active_flag_response.json()["data"]["status"] == "allowed"
    assert active_flag_response.json()["data"]["model"]["active_flag"] is False
    assert store.calls == [("model_1", True), ("model_1", False)]


def test_model_lifecycle_contract_returns_preflight_audit_and_lifecycle_state() -> None:
    store = _ModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            preflight = client.post(
                "/api/v1/models/inactive_model/preflight",
                json={"operation": "activate"},
                headers={"X-User-Role": "model_admin"},
            )
            lifecycle = client.post(
                "/api/v1/models/inactive_model/lifecycle",
                json={"operation": "activate"},
                headers={"X-User-Role": "model_admin"},
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert preflight.status_code == 200
    preflight_data = _assert_success_envelope(preflight.json())
    assert preflight_data["schema"] == "nhms.model_operation_preflight.v1"
    assert preflight_data["operation"] == "activate"
    assert preflight_data["status"] == "ready"
    assert lifecycle.status_code == 200
    data = _assert_success_envelope(lifecycle.json())
    assert data["status"] == "allowed"
    assert data["operation"] == "activate"
    assert data["model"]["lifecycle_state"] == "active"
    assert data["preflight"]["status"] == "ready"
    assert data["audit_reference"]["entity_type"] == "model_instance"


def test_model_active_requires_model_admin_before_mutation() -> None:
    store = _ModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            missing = client.put("/api/v1/models/model_1/active", json={"active": True})
            forbidden = client.put(
                "/api/v1/models/model_1/active",
                json={"active": True},
                headers={"X-User-Role": "operator"},
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "AUTH_REQUIRED"
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "RBAC_FORBIDDEN"
    assert store.calls == []


def test_model_list_contract_uses_page_envelope_and_active_values() -> None:
    app.dependency_overrides[get_model_registry_store] = lambda: _ModelRegistryStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/models", params={"active": "all", "limit": 10, "offset": 0})
            limit_boundary_response = client.get("/api/v1/models", params={"limit": 501})
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert response.status_code == 200
    assert limit_boundary_response.status_code == 422
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total", "limit", "offset"}
    assert data["total"] == 2
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert {item["model_id"] for item in data["items"]} == {"active_model", "inactive_model"}
    inactive_item = next(item for item in data["items"] if item["model_id"] == "inactive_model")
    assert inactive_item["resource_profile"]["manifest_uri"] == "s3://nhms/models/inactive_model/vbasins/manifest.json"
    public_listing_json = json.dumps(inactive_item)
    for token in (
        "/volume/data",
        "C:\\",
        "file://",
        "token=secret",
        "user:pass@",
        "#frag",
        "package-sha-1",
        "inventory-sha-1",
        "mesh-sha-1",
    ):
        assert token not in public_listing_json

    spec = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text())
    list_models = spec["paths"]["/api/v1/models"]["get"]
    active_parameter = next(parameter for parameter in list_models["parameters"] if parameter.get("name") == "active")
    assert {"type": "string", "enum": ["true", "false", "all"], "default": "true"}.items() <= active_parameter[
        "schema"
    ].items()
    limit_parameter = next(parameter for parameter in list_models["parameters"] if parameter.get("name") == "limit")
    assert limit_parameter["schema"]["maximum"] == 500


def test_basin_version_list_redacts_source_uri_and_checksum() -> None:
    store = _ModelRegistryStore()
    store.basin_versions[0]["source_uri"] = "/volume/data/nwm/Basins/qhh/gis/domain.shp"
    store.basin_versions[0]["checksum"] = "checksum-secret"
    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/basins/basins_basin_a/versions")
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert data[0]["source_uri"] is None
    assert data[0]["checksum"] is None
    rendered = json.dumps(data)
    assert "/volume/data" not in rendered
    assert "checksum-secret" not in rendered


def test_basins_has_display_product_flag_reaches_store() -> None:
    class _RecordingBasinStore:
        """Captures the has_display_product flag actually delivered to the store."""

        def __init__(self) -> None:
            self.has_display_product_calls: list[bool] = []

        def list_basins(self, *, limit: int, offset: int, has_display_product: bool = False) -> list[dict[str, Any]]:
            self.has_display_product_calls.append(has_display_product)
            return []

    store = _RecordingBasinStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        with TestClient(app) as client:
            filtered = client.get("/api/v1/basins", params={"has_display_product": "true"})
            default = client.get("/api/v1/basins")
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert filtered.status_code == 200
    assert default.status_code == 200
    # First request opted in, second relied on the default.
    assert store.has_display_product_calls == [True, False]


def test_model_detail_contract_exposes_basins_asset_metadata() -> None:
    store = _ModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/models/inactive_model")
            missing_response = client.get("/api/v1/models/missing_model")
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert {
        "model_id": "inactive_model",
        "model_name": "alias-a",
        "basin_id": "basins_basin_a",
        "basin_name": "Basin A",
        "basin_version_id": "basin_v1",
        "river_network_version_id": "network_v1",
        "mesh_version_id": "mesh_v1",
        "calibration_version_id": "calibration_v1",
        "segment_count": 2,
        "mesh_uri": "s3://nhms/models/inactive_model/vbasins/package/alias-a.sp.mesh",
        "model_package_uri": "s3://nhms/models/inactive_model/package/",
        "active_flag": False,
        "manifest_uri": "s3://nhms/models/inactive_model/vbasins/manifest.json",
        "basin_slug": "basin-a",
        "shud_input_name": "alias-a",
        "source_uri": "s3://nhms/sources/basin-a",
        "source_is_symlink": False,
    }.items() <= data.items()
    assert data["mesh_checksum"] is None
    assert data["package_checksum"] is None
    assert data["source_inventory_checksum"] is None
    assert data["source_path"] is None
    assert data["resolved_source_path"] is None
    assert data["resource_profile"]["manifest_uri"] == "s3://nhms/models/inactive_model/vbasins/manifest.json"
    assert data["resource_profile"]["source_uri"] == "s3://nhms/sources/basin-a"
    assert data["resource_profile"]["lineage"]["source_uris"] == [
        "s3://nhms/sources/nested",
        None,
    ]
    assert data["resource_profile"]["lineage"]["note"] == "s3 label only"
    public_detail_json = json.dumps(data)
    for token in (
        "/volume/data",
        "C:\\",
        "file://",
        "token=secret",
        "user:pass@",
        "#frag",
        "package-sha-1",
        "inventory-sha-1",
        "mesh-sha-1",
    ):
        assert token not in public_detail_json

    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "MODEL_REGISTRY_NOT_FOUND"


def test_river_segment_geojson_budget_error_contract() -> None:
    store = _OversizedRiverSegmentStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        with TestClient(app) as client:
            collection_response = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments",
                params={"river_network_version_id": "network_v1"},
            )
            detail_response = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_1",
                params={"river_network_version_id": "network_v1"},
            )
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    for response, scope in ((collection_response, "collection"), (detail_response, "detail")):
        assert response.status_code == 413
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "RIVER_SEGMENT_GEOJSON_BUDGET_EXCEEDED"
        assert body["error"]["details"]["limit_type"] == "serialized_bytes"
        assert body["error"]["details"]["scope"] == scope

    spec = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text())
    collection_responses = spec["paths"]["/api/v1/basin-versions/{basin_version_id}/river-segments"]["get"][
        "responses"
    ]
    detail_responses = spec["paths"]["/api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}"]["get"][
        "responses"
    ]
    assert collection_responses["413"]["description"] == "River segment GeoJSON payload budget exceeded."
    assert detail_responses["413"]["description"] == "River segment GeoJSON payload budget exceeded."


def test_river_segments_reversed_stream_order_range_rejected() -> None:
    store = _ModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments",
                params={"stream_order_min": 5, "stream_order_max": 2},
            )
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["stream_order_min"] == 5
    assert body["error"]["details"]["stream_order_max"] == 2


def test_forecast_series_contract_accepts_include_analysis_query() -> None:
    app.dependency_overrides[get_forecast_store] = lambda: _ForecastSeriesStore()
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_1/forecast-series",
                params={"river_network_version_id": "network_v1", "include_analysis": "true", "run_types": "forecast"},
            )
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    data = response.json()
    assert data["river_segment_id"] == "seg_1"
    assert data["segments"] == [
        {
            "scenario": "analysis_true_field",
            "source": "ERA5",
            "data": [{"valid_time": "2026-05-14T00:00:00Z", "value": 10.0}],
        }
    ]
