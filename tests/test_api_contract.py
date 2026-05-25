from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes.data_sources import get_data_source_store
from apps.api.routes.forecast import get_forecast_store
from apps.api.routes.models import get_model_registry_store
from packages.common.forecast_store import ForecastStoreError
from tests.test_monitoring_api import (
    _client,
    _create_job,
    _cycle_time,
    _insert_cycle,
    _MockGateway,
    _seed_monitoring_jobs,
    _store,
)
from workers.data_adapters.base import cycle_id_for

PIPELINE_JOB_KEYS = {
    "job_id",
    "run_id",
    "cycle_id",
    "run_type",
    "scenario",
    "job_type",
    "slurm_job_id",
    "model_id",
    "status",
    "stage",
    "submitted_at",
    "started_at",
    "finished_at",
    "exit_code",
    "retry_count",
    "error_code",
    "error_message",
    "log_uri",
    "duration_seconds",
}


def test_runs_contract_uses_success_envelope_and_paginated_data() -> None:
    app.dependency_overrides[get_forecast_store] = lambda: _RunStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs", params={"status": "frequency_done", "limit": 10, "offset": 0})
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total_count", "limit", "offset", "total"}
    assert data["total"] == 1
    assert data["total_count"] == 1
    assert data["limit"] == 10
    assert data["offset"] == 0
    run = data["items"][0]
    assert run["run_id"] == "run_frequency_done"
    assert run["run_type"] == "forecast"
    assert run["status"] == "frequency_done"
    assert run["river_network_version_id"] == "network_v1"
    assert run["product_quality"]["flood_return_period"]["quality_state"] == "ready"
    assert isinstance(run["start_time"], str)
    assert isinstance(run["end_time"], str)


def test_jobs_contract_uses_success_envelope_and_paginated_pipeline_jobs() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 2, "offset": 0},
            )

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total", "limit", "offset"}
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0
    assert len(data["items"]) == 2
    assert set(data["items"][0]) == PIPELINE_JOB_KEYS
    assert data["items"][0]["run_type"] is None
    assert data["items"][0]["scenario"] is None


def test_pipeline_status_contract_uses_success_envelope() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _insert_cycle(store, cycle_time=cycle_time, current_state="forecast_running")
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/pipeline/status",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"cycle_id", "source", "cycle_time", "current_state", "started_at", "updated_at", "job_counts"}
    assert data["cycle_id"] == cycle_id
    assert data["current_state"] == "forecast_running"
    assert data["job_counts"] == {"succeeded": 3, "failed": 1, "running": 1, "pending": 0}


def test_queue_depth_contract_uses_success_envelope() -> None:
    with _store() as store:
        with _client(store, _MockGateway(depth={"running": 2, "pending": 3, "idle": 1})) as client:
            response = client.get("/api/v1/queue/depth")

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"running", "pending", "idle"}
    assert data == {"running": 2, "pending": 3, "idle": 1}


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


def test_met_station_series_contract_uses_success_envelope_and_store_payload() -> None:
    store = _DataSourceStore()
    app.dependency_overrides[get_data_source_store] = lambda: store
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/met/stations/station_1/series",
                params={
                    "forcing_version_id": "forc_qhh_gfs_2026050700",
                    "variables": "PRCP",
                    "from": "2026-05-07T00:00:00Z",
                    "to": "2026-05-07T03:00:00Z",
                    "limit": 2,
                },
            )
            missing = client.get(
                "/api/v1/met/stations/station_1/series",
                params={"forcing_version_id": "missing"},
            )
    finally:
        app.dependency_overrides.pop(get_data_source_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "station_id",
        "station",
        "forcing_version_id",
        "model_id",
        "source_id",
        "cycle_time",
        "valid_time_start",
        "valid_time_end",
        "limit",
        "requested_from",
        "requested_to",
        "series",
    }
    assert data["station"]["station_id"] == "station_1"
    assert data["forcing_version_id"] == "forc_qhh_gfs_2026050700"
    assert data["source_id"] == "GFS"
    assert data["series"][0]["unit"] == "mm/h"
    assert data["series"][0]["native_resolution"] == "1h"
    assert data["series"][0]["points"][0]["quality_flag"] == "ok"
    assert data["series"][0]["metadata"]["truncated"] is True
    assert store.station_series_calls[0]["variables"] == ["PRCP"]
    assert missing.status_code == 404
    assert missing.json()["status"] == "error"
    assert missing.json()["error"]["code"] == "FORCING_VERSION_NOT_FOUND"
    assert missing.json()["error"]["details"] == {"forcing_version_id": "missing"}


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
    assert active_parameter["schema"] == {
        "type": "string",
        "enum": ["true", "false", "all"],
        "default": "true",
    }
    limit_parameter = next(parameter for parameter in list_models["parameters"] if parameter.get("name") == "limit")
    assert limit_parameter["schema"]["maximum"] == 500
    response_schema = list_models["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["allOf"][1]["properties"]["data"]["$ref"] == "#/components/schemas/ModelInstancePage"


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

    spec = yaml.safe_load((Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml").read_text())
    get_model = spec["paths"]["/api/v1/models/{model_id}"]["get"]
    response_schema = get_model["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_schema["allOf"][1]["properties"]["data"]["$ref"] == "#/components/schemas/ModelInstance"
    model_properties = spec["components"]["schemas"]["ModelInstance"]["properties"]
    for field in (
        "model_name",
        "basin_id",
        "basin_name",
        "segment_count",
        "mesh_uri",
        "mesh_checksum",
        "package_checksum",
        "manifest_uri",
        "source_inventory_checksum",
        "basin_slug",
        "shud_input_name",
        "source_path",
        "resolved_source_path",
        "source_uri",
        "source_is_symlink",
    ):
        assert field in model_properties


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
    assert collection_responses["413"]["$ref"] == "#/components/responses/Error"
    assert detail_responses["413"]["$ref"] == "#/components/responses/Error"


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


def test_stage_duration_metrics_contract_uses_success_envelope() -> None:
    with _store() as store:
        cycle_id = cycle_id_for("GFS", _cycle_time())
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get("/api/v1/metrics/stage-duration", params={"days": 30})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert isinstance(data, list)
    metric = next(row for row in data if row["stage"] == "download")
    assert set(metric) == {"date", "stage", "average_duration_seconds", "job_count"}
    assert isinstance(metric["date"], str)
    assert isinstance(metric["average_duration_seconds"], float)
    assert isinstance(metric["job_count"], int)


def test_retry_contract_documents_pipeline_job_and_execution_status_fields() -> None:
    with _store() as store:
        _create_job(store, job_id="job_retry_contract", run_id="run_retry_contract", status="failed")
        with _client(store, _RetryGateway(), allow_dev_role_header=True) as client:
            response = client.post("/api/v1/runs/run_retry_contract/retry", headers={"X-User-Role": "operator"})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "retry_count",
        "status",
        "slurm_job_id",
        "execution_status",
    }
    assert data["pipeline_job_id"] == data["job_id"]
    assert data["run_id"] == "run_retry_contract"
    assert data["execution_status"] == "submitted"
    assert data["slurm_job_id"] == "slurm_retry_contract"


def test_cancel_contract_documents_cancelled_jobs_and_slurm_failures() -> None:
    with _store() as store:
        gateway = _MockGateway()
        _create_job(
            store,
            job_id="job_cancel_contract",
            run_id="run_cancel_contract",
            status="running",
            slurm_job_id="slurm_cancel_contract",
        )
        with _client(store, gateway, allow_dev_role_header=True) as client:
            response = client.post("/api/v1/runs/run_cancel_contract/cancel", headers={"X-User-Role": "operator"})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "run_id",
        "cancelled_jobs",
        "cancelled",
        "failed_jobs",
        "slurm_failures",
        "blocked_jobs",
        "slurm_cancellation_gaps",
        "partial_failure",
        "idempotent_jobs",
        "hydro_run",
        "forecast_cycle",
    }
    assert data["run_id"] == "run_cancel_contract"
    assert data["failed_jobs"] == []
    assert data["slurm_failures"] == []
    assert data["blocked_jobs"] == []
    assert data["slurm_cancellation_gaps"] == []
    assert data["partial_failure"] is False
    assert data["cancelled"] == data["cancelled_jobs"]
    assert len(data["cancelled_jobs"]) == 1
    assert set(data["cancelled_jobs"][0]) == PIPELINE_JOB_KEYS
    assert data["cancelled_jobs"][0]["status"] == "cancelled"


def test_generated_frontend_types_match_openapi(tmp_path: Path) -> None:
    generated = tmp_path / "generated-types.ts"
    subprocess.run(
        [
            "npx",
            "openapi-typescript",
            "../../openapi/nhms.v1.yaml",
            "--output",
            str(generated),
        ],
        cwd=Path(__file__).resolve().parents[1] / "apps" / "frontend",
        check=True,
    )
    committed = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    assert committed.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8")


def test_generated_frontend_types_include_model_page_and_flood_threshold_shapes() -> None:
    types_path = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    generated_types = types_path.read_text(encoding="utf-8")

    assert "active?: \"true\" | \"false\" | \"all\";" in generated_types
    assert 'data: components["schemas"]["ModelInstancePage"];' in generated_types
    assert 'data: components["schemas"]["ModelInstance"];' in generated_types
    assert "model_name?: string | null;" in generated_types
    assert "segment_count?: number | null;" in generated_types
    assert "mesh_uri?: string | null;" in generated_types
    assert "package_checksum?: string | null;" in generated_types
    assert "source_inventory_checksum?: string | null;" in generated_types
    assert "source_path?: string | null;" in generated_types
    assert "resolved_source_path?: string | null;" in generated_types
    assert "source_uri?: string | null;" in generated_types
    assert "source_is_symlink?: boolean | null;" in generated_types
    assert "restored_model_id?: string | null;" in generated_types
    assert "FloodFrequencyThresholds" in generated_types
    assert "Q2?: number | null;" in generated_types
    assert "Q20?: number | null;" in generated_types
    assert "Q100?: number | null;" in generated_types
    assert "frequency_thresholds: {" not in generated_types
    assert "frequency_thresholds: components[\"schemas\"][\"FloodFrequencyThresholds\"] | null;" in generated_types
    assert "frequency_thresholds?: components[\"schemas\"][\"FloodFrequencyThresholds\"] | null;" in generated_types
    assert "frequency_thresholds: Record<string, never> | null;" not in generated_types
    assert "frequency_thresholds?: Record<string, never> | null;" not in generated_types


def test_station_series_openapi_and_generated_types_include_store_contract() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    operation = spec["paths"]["/api/v1/met/stations/{station_id}/series"]["get"]
    parameters: dict[str, dict[str, Any]] = {}
    for parameter in operation["parameters"]:
        if "$ref" in parameter:
            parameter = spec["components"]["parameters"][parameter["$ref"].removeprefix("#/components/parameters/")]
        parameters[parameter["name"]] = parameter

    assert operation["operationId"] == "getMetStationSeries"
    assert set(parameters) == {
        "station_id",
        "forcing_version_id",
        "model_id",
        "source_id",
        "cycle_time",
        "variables",
        "from",
        "to",
        "limit",
    }
    assert parameters["variables"]["schema"] == {
        "oneOf": [
            {"type": "string"},
            {"type": "array", "items": {"type": "string"}},
        ]
    }
    for name in ("forcing_version_id", "model_id", "source_id"):
        assert parameters[name]["schema"] == {"type": "string", "minLength": 1}
    assert parameters["limit"]["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 10000,
    }
    response_data = operation["responses"]["200"]["content"]["application/json"]["schema"]["allOf"][1]["properties"][
        "data"
    ]
    assert response_data["$ref"] == "#/components/schemas/StationSeriesResponse"
    schemas = spec["components"]["schemas"]
    assert schemas["StationSeriesResponse"]["required"] == [
        "station_id",
        "station",
        "forcing_version_id",
        "source_id",
        "limit",
        "series",
    ]
    assert "quality_flag" in schemas["StationSeriesPoint"]["properties"]
    assert "native_resolution" in schemas["StationSeries"]["properties"]
    assert "returned_points" in schemas["StationSeriesMetadata"]["properties"]

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    operation_start = generated_types.index("getMetStationSeries:")
    list_runs_start = generated_types.index("listRuns:")
    operation_types = generated_types[operation_start:list_runs_start]
    assert "forcing_version_id?: string;" in operation_types
    assert "model_id?: string;" in operation_types
    assert "source_id?: string;" in operation_types
    assert "cycle_time?: string;" in operation_types
    assert "variables?: string | string[];" in operation_types
    assert "from?: string;" in operation_types
    assert "to?: string;" in operation_types
    assert "limit?: number;" in operation_types
    assert 'data: components["schemas"]["StationSeriesResponse"];' in operation_types
    assert "StationSeriesResponse:" in generated_types
    assert "StationSeriesPoint:" in generated_types
    assert "quality_flag: string | null;" in generated_types
    assert "native_resolution: string | null;" in generated_types


def test_flood_product_quality_contract_is_in_static_openapi_and_types() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    run_parameters = spec["paths"]["/api/v1/runs"]["get"]["parameters"]
    ready_filter = next(parameter for parameter in run_parameters if parameter.get("name") == "flood_product_ready")
    assert ready_filter["schema"]["type"] == "boolean"
    assert spec["components"]["schemas"]["HydroRun"]["properties"]["product_quality"] == {
        "type": "object",
        "additionalProperties": True,
        "nullable": True,
        "description": "Product readiness evidence keyed by product family, including flood_return_period readiness.",
    }
    assert spec["components"]["schemas"]["FloodReturnPeriodFeatureCollection"]["properties"]["product_quality"] == {
        "type": "object",
        "additionalProperties": True,
        "nullable": True,
        "description": "Flood return-period readiness evidence for the selected run.",
    }

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    runs_start = generated_types.index("listRuns:")
    get_run_start = generated_types.index("getRun:")
    assert "flood_product_ready?: boolean;" in generated_types[runs_start:get_run_start]
    hydro_run_start = generated_types.index("HydroRun:")
    hydro_page_start = generated_types.index("HydroRunPage:")
    assert "product_quality?: {" in generated_types[hydro_run_start:hydro_page_start]
    collection_start = generated_types.index("FloodReturnPeriodFeatureCollection:")
    feature_start = generated_types.index("FloodReturnPeriodFeature:")
    assert "product_quality?: {" in generated_types[collection_start:feature_start]


def test_flood_alert_ranking_and_timeline_bounds_are_in_static_contract_and_types() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    ranking_parameters = spec["paths"]["/api/v1/flood-alerts/ranking"]["get"]["parameters"]
    ranking_limit = next(parameter for parameter in ranking_parameters if parameter.get("name") == "limit")
    assert ranking_limit["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 200,
        "default": 10,
    }

    timeline_parameters = spec["paths"]["/api/v1/flood-alerts/timeline"]["get"]["parameters"]
    timeline_max_points = next(parameter for parameter in timeline_parameters if parameter.get("name") == "max_points")
    assert timeline_max_points["schema"] == {
        "type": "integer",
        "minimum": 1,
        "maximum": 1000,
        "default": 168,
    }

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    ranking_start = generated_types.index("listFloodAlertRanking:")
    segments_start = generated_types.index("listFloodAlertSegments:")
    ranking_types = generated_types[ranking_start:segments_start]
    assert 'limit?: number;' in ranking_types
    assert 'limit?: components["parameters"]["Limit"];' not in ranking_types
    ranking_item_schema = spec["components"]["schemas"]["FloodAlertRankingItem"]
    assert "geom_centroid" in ranking_item_schema["required"]
    assert ranking_item_schema["properties"]["geom_centroid"] == {
        "type": "object",
        "nullable": True,
        "allOf": [{"$ref": "#/components/schemas/GeoJSONPoint"}],
        "description": "GeoJSON point centroid, or null",
    }
    assert 'geom_centroid: components["schemas"]["GeoJSONPoint"] | null;' in generated_types

    timeline_start = generated_types.index("getFloodAlertTimeline:")
    lineage_start = generated_types.index("getRiverPointLineage:")
    timeline_types = generated_types[timeline_start:lineage_start]
    assert "max_points?: number;" in timeline_types

    lineage_parameters = spec["paths"]["/api/v1/lineage/river-point"]["get"]["parameters"]
    lineage_river_network = next(
        parameter for parameter in lineage_parameters if parameter.get("name") == "river_network_version_id"
    )
    assert lineage_river_network["required"] is True
    assert lineage_river_network["schema"] == {
        "type": "string",
        "minLength": 1,
    }

    forcing_lineage_start = generated_types.index("getForcingPointLineage:")
    lineage_types = generated_types[lineage_start:forcing_lineage_start]
    assert "river_network_version_id: string;" in lineage_types


def test_forecast_response_issue_time_contract_allows_runtime_nulls() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]

    for schema_name in ("RiverSeriesResponse", "SplicedForecastResponse"):
        issue_time = schemas[schema_name]["properties"]["issue_time"]
        assert issue_time == {
            "type": "string",
            "format": "date-time",
            "nullable": True,
        }

    types_path = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    generated_types = types_path.read_text(encoding="utf-8")
    river_start = generated_types.index("RiverSeriesResponse:")
    spliced_start = generated_types.index("SplicedForecastResponse:")
    series_segment_start = generated_types.index("SeriesSegment:")
    assert "issue_time: string | null;" in generated_types[river_start:spliced_start]
    assert "issue_time: string | null;" in generated_types[spliced_start:series_segment_start]


def test_spliced_forecast_segment_metadata_is_in_public_contract() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    segment_schema = spec["components"]["schemas"]["SplicedForecastResponse"]["properties"]["segments"]["items"]

    assert "segment_role" in segment_schema["required"]
    properties = segment_schema["properties"]
    assert properties["scenario_id"]["type"] == "string"
    assert properties["source_id"]["nullable"] is True
    assert properties["cycle_time"] == {
        "type": "string",
        "format": "date-time",
        "nullable": True,
        "description": "Forecast source cycle time when available.",
    }
    assert properties["available_lead_hours"]["type"] == "integer"
    assert properties["available_lead_hours"]["nullable"] is True
    assert properties["segment_role"]["enum"] == ["past_7_days", "future_7_days"]

    types_path = Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    generated_types = types_path.read_text(encoding="utf-8")
    spliced_start = generated_types.index("SplicedForecastResponse:")
    series_segment_start = generated_types.index("SeriesSegment:")
    spliced_types = generated_types[spliced_start:series_segment_start]
    assert "scenario_id?: string;" in spliced_types
    assert "source_id?: string | null;" in spliced_types
    assert "cycle_time?: string | null;" in spliced_types
    assert "available_lead_hours?: number | null;" in spliced_types
    assert 'segment_role: "past_7_days" | "future_7_days";' in spliced_types


def test_river_series_threshold_schema_allows_null_and_empty_thresholds() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]

    threshold_schema = schemas["FloodFrequencyThresholds"]
    assert "required" not in threshold_schema
    assert threshold_schema["properties"]["Q20"]["nullable"] is True

    river_thresholds = schemas["RiverSeriesResponse"]["properties"]["frequency_thresholds"]
    assert river_thresholds == {
        "type": "object",
        "allOf": [{"$ref": "#/components/schemas/FloodFrequencyThresholds"}],
        "nullable": True,
    }


def _assert_success_envelope(body: dict[str, Any]) -> Any:
    assert {"request_id", "status", "data"} <= set(body)
    assert body["request_id"]
    assert body["status"] == "ok"
    return body["data"]


class _RetryGateway(_MockGateway):
    def submit_job(self, request: Any) -> dict[str, Any]:
        return {
            "job_id": "slurm_retry_contract",
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }


class _RunStore:
    def list_runs(self, **kwargs: Any) -> dict[str, Any]:
        now = datetime(2026, 5, 3, tzinfo=UTC)
        return {
            "items": [
                {
                    "run_id": "run_frequency_done",
                    "run_type": "forecast",
                    "scenario_id": "forecast_gfs_deterministic",
                    "model_id": "model_1",
                    "basin_version_id": "basin_v1",
                    "river_network_version_id": "network_v1",
                    "forcing_version_id": None,
                    "init_state_id": None,
                    "source_id": "GFS",
                    "cycle_time": now.isoformat(),
                    "status": kwargs.get("status") or "frequency_done",
                    "slurm_job_id": None,
                    "start_time": now.isoformat(),
                    "end_time": (now + timedelta(days=7)).isoformat(),
                    "run_manifest_uri": "object://manifest",
                    "output_uri": None,
                    "log_uri": None,
                    "error_code": None,
                    "error_message": None,
                    "product_quality": {
                        "flood_return_period": {
                            "quality_state": "ready",
                            "max_over_window": True,
                            "result_rows": 2,
                            "return_period_rows": 2,
                            "warning_rows": 2,
                            "unavailable_products": [],
                            "residual_blockers": [],
                        }
                    },
                    "created_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }


class _DataSourceStore:
    def __init__(self) -> None:
        self.station_series_calls: list[dict[str, Any]] = []

    def list_data_sources(self, *, limit: int, offset: int) -> dict[str, Any]:
        return {
            "items": [{"source_id": "GFS", "provider": "NOAA/NCEP", "format": "GRIB2"}],
            "total_count": 1,
            "limit": limit,
            "offset": offset,
        }

    def list_cycles(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "cycle_id": f"{kwargs['source_id']}_2026051400",
                    "source_id": kwargs["source_id"],
                    "status": "raw_complete",
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    def list_met_stations(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "station_id": "station_1",
                    "basin_version_id": kwargs["basin_version_id"],
                    "active_flag": True,
                }
            ],
            "total_count": 1,
            "limit": kwargs["limit"],
            "offset": kwargs["offset"],
        }

    def station_series(self, **kwargs: Any) -> dict[str, Any]:
        self.station_series_calls.append(kwargs)
        if kwargs.get("forcing_version_id") == "missing":
            raise ForecastStoreError(
                status_code=404,
                code="FORCING_VERSION_NOT_FOUND",
                message="Forcing version not found: missing",
                details={"forcing_version_id": "missing"},
            )
        return {
            "station_id": kwargs["station_id"],
            "station": {
                "station_id": kwargs["station_id"],
                "basin_version_id": "basin_v1",
                "station_name": "Station 1",
                "name": "Station 1",
                "longitude": 101.0,
                "latitude": 36.0,
                "elevation_m": 3200.0,
                "elevation": 3200.0,
                "station_role": "forcing_proxy",
                "active_flag": True,
                "properties_json": {"source": "fixture"},
            },
            "forcing_version_id": kwargs.get("forcing_version_id") or "forc_qhh_gfs_2026050700",
            "model_id": "qhh_shud_v1",
            "source_id": "GFS",
            "cycle_time": "2026-05-07T00:00:00Z",
            "valid_time_start": "2026-05-07T00:00:00Z",
            "valid_time_end": "2026-05-14T00:00:00Z",
            "limit": kwargs["limit"],
            "requested_from": "2026-05-07T00:00:00Z",
            "requested_to": "2026-05-07T03:00:00Z",
            "series": [
                {
                    "variable": "PRCP",
                    "unit": "mm/h",
                    "native_resolution": "1h",
                    "source_id": "GFS",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "points": [
                        {
                            "valid_time": "2026-05-07T00:00:00Z",
                            "value": 1.0,
                            "quality_flag": "ok",
                            "source_id": "GFS",
                        }
                    ],
                    "truncated": True,
                    "metadata": {
                        "limit": kwargs["limit"],
                        "returned_points": 1,
                        "requested_from": "2026-05-07T00:00:00Z",
                        "requested_to": "2026-05-07T03:00:00Z",
                        "returned_from": "2026-05-07T00:00:00Z",
                        "returned_to": "2026-05-07T00:00:00Z",
                        "truncated": True,
                    },
                }
            ],
        }


class _ModelRegistryStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.models = [
            {
                "model_id": "active_model",
                "model_name": "active_model",
                "basin_id": "basin",
                "basin_name": "Basin",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "network_v1",
                "mesh_version_id": "mesh_v1",
                "calibration_version_id": "calibration_v1",
                "shud_code_version": "2.0",
                "segment_count": 1,
                "mesh_uri": "s3://nhms/models/active_model/package/active.sp.mesh",
                "mesh_checksum": "mesh-sha-active",
                "model_package_uri": "s3://nhms/models/active_model/package/",
                "package_checksum": None,
                "manifest_uri": None,
                "source_inventory_checksum": None,
                "basin_slug": None,
                "shud_input_name": None,
                "source_path": None,
                "resolved_source_path": None,
                "source_uri": None,
                "source_is_symlink": None,
                "active_flag": True,
                "lifecycle_state": "active",
                "resource_profile": {},
                "created_at": "2026-05-14T00:00:00Z",
            },
            {
                "model_id": "inactive_model",
                "model_name": "alias-a",
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "network_v1",
                "mesh_version_id": "mesh_v1",
                "calibration_version_id": "calibration_v1",
                "shud_code_version": "2.0",
                "segment_count": 2,
                "mesh_uri": "s3://nhms/models/inactive_model/vbasins/package/alias-a.sp.mesh",
                "mesh_checksum": "mesh-sha-1",
                "model_package_uri": "s3://nhms/models/inactive_model/package/",
                "package_checksum": "package-sha-1",
                "manifest_uri": "s3://nhms/models/inactive_model/vbasins/manifest.json",
                "source_inventory_checksum": "inventory-sha-1",
                "basin_slug": "basin-a",
                "shud_input_name": "alias-a",
                "source_path": "/volume/data/nwm/Basins/basin-a",
                "resolved_source_path": "/volume/data/nwm/Basins/basin-a",
                "source_uri": "s3://nhms/sources/basin-a",
                "source_is_symlink": False,
                "active_flag": False,
                "lifecycle_state": "inactive",
                "resource_profile": {
                    "manifest_uri": "s3://nhms/models/inactive_model/vbasins/manifest.json",
                    "source_uri": "s3://nhms/sources/basin-a",
                    "lineage": {
                        "source_uris": [
                            "s3://nhms/sources/nested",
                            "/volume/data/nwm/Basins/local-source",
                        ],
                        "note": "s3 label only",
                    },
                },
                "created_at": "2026-05-14T00:00:00Z",
            },
        ]
        self.basin_versions = [
            {
                "basin_version_id": "basins_basin_a_vbasins",
                "basin_id": "basins_basin_a",
                "version_label": "vbasins",
                "geom": {"type": "MultiPolygon", "coordinates": []},
                "active_flag": True,
                "valid_from": None,
                "valid_to": None,
                "source_uri": None,
                "checksum": None,
                "created_at": "2026-05-14T00:00:00Z",
            }
        ]

    def set_model_active(self, model_id: str, active: bool, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append((model_id, active))
        return {
            "model_id": model_id,
            "basin_version_id": "basin_v1",
            "river_network_version_id": "network_v1",
            "mesh_version_id": "mesh_v1",
            "calibration_version_id": "calibration_v1",
            "shud_code_version": "2.0",
            "model_package_uri": "s3://nhms/models/model_1/package/",
            "active_flag": active,
            "lifecycle_state": "active" if active else "inactive",
            "resource_profile": {},
            "created_at": "2026-05-14T00:00:00Z",
        }

    def preflight_model_operation(self, model_id: str, *, operation: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "schema": "nhms.model_operation_preflight.v1",
            "request_id": "contract",
            "operation": operation,
            "status": "ready",
            "model_id": model_id,
            "basin_version_id": "basin_v1",
            "blockers": [],
            "warnings": [],
            "impact": {"downstream_surfaces": ["forecast-routing"]},
        }

    def model_lifecycle_operation(self, model_id: str, *, operation: str, **_kwargs: Any) -> dict[str, Any]:
        model = self.set_model_active(model_id, operation in {"activate", "switch_version", "rollback_version"})
        return {
            "status": "allowed",
            "operation": operation,
            "model": model,
            "preflight": self.preflight_model_operation(model_id, operation=operation),
            "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": 7},
        }

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        del basin_version_id
        items = self.models
        if active is not None:
            items = [item for item in items if item["active_flag"] == active]
        return {"items": items[offset : offset + limit], "total": len(items), "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        for item in self.models:
            if item["model_id"] == model_id:
                return dict(item)
        from packages.common.model_registry import MissingResourceError

        raise MissingResourceError(f"model_id not found: {model_id}")

    def list_basins(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        return [
            {
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "basin_group": None,
                "description": None,
                "created_at": "2026-05-14T00:00:00Z",
            }
        ][offset : offset + limit]

    def list_basin_versions(self, *, basin_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        del basin_id
        return [dict(item) for item in self.basin_versions[offset : offset + limit]]


class _OversizedRiverSegmentStore(_ModelRegistryStore):
    def list_river_segments(self, **_kwargs: Any) -> dict[str, Any]:
        from packages.common.model_registry import RiverSegmentGeoJsonBudgetError

        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=100,
            serialized_bytes=101,
            scope="collection",
        )

    def get_river_segment(self, **_kwargs: Any) -> dict[str, Any]:
        from packages.common.model_registry import RiverSegmentGeoJsonBudgetError

        raise RiverSegmentGeoJsonBudgetError(
            limit_type="serialized_bytes",
            max_bytes=100,
            serialized_bytes=101,
            scope="detail",
        )


class _ForecastSeriesStore:
    def forecast_series(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["include_analysis"] is True
        assert kwargs["run_types"] == ["forecast"]
        assert kwargs["river_network_version_id"] == "network_v1"
        return {
            "segments": [
                {
                    "scenario": "analysis_true_field",
                    "source": "ERA5",
                    "data": [{"valid_time": "2026-05-14T00:00:00Z", "value": 10.0}],
                }
            ],
            "issue_time": "2026-05-14T00:00:00Z",
            "river_segment_id": kwargs["segment_id"],
            "variable": "discharge",
            "unit": "m3/s",
        }
