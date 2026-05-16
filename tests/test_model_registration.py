from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.models import get_model_registry_store
from packages.common.model_registry import (
    DuplicateResourceError,
    InvalidPayloadError,
    InvalidReferenceError,
    MissingResourceError,
    ModelRegistryError,
    PsycopgModelRegistryStore,
    geometry_to_wkt,
)
from workers.model_registry.cli import _argparse_main
from workers.model_registry.validator import (
    ModelPackageValidationError,
    validate_model_package_path,
    validate_model_package_uri,
)


class FakeModelRegistryStore:
    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {
            "inactive_model": {
                "model_id": "inactive_model",
                "model_name": "basin_a",
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "mesh_uri": "s3://nhms/models/inactive_model/v1/package/basin_a.sp.mesh",
                "mesh_checksum": "mesh-sha-1",
                "model_package_uri": "s3://nhms/models/inactive_model/package/",
                "package_checksum": "package-sha-1",
                "manifest_uri": "s3://nhms/models/inactive_model/v1/manifest.json",
                "source_inventory_checksum": "inventory-sha-1",
                "basin_slug": "basin-a",
                "shud_input_name": "basin_a",
                "segment_count": 2,
                "active_flag": False,
                "resource_profile": {
                    "basin_slug": "basin-a",
                    "shud_input_name": "basin_a",
                    "manifest_uri": "s3://nhms/models/inactive_model/v1/manifest.json",
                    "package_checksum": "package-sha-1",
                    "source_inventory_checksum": "inventory-sha-1",
                },
                "created_at": "2026-05-07T00:00:00Z",
            },
            "active_model": {
                "model_id": "active_model",
                "model_name": "active_model",
                "basin_id": "basin",
                "basin_name": "Basin",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "mesh_uri": "s3://nhms/models/active_model/package/demo.sp.mesh",
                "mesh_checksum": "mesh-sha-active",
                "model_package_uri": "s3://nhms/models/active_model/package/",
                "package_checksum": None,
                "manifest_uri": None,
                "source_inventory_checksum": None,
                "basin_slug": None,
                "shud_input_name": None,
                "segment_count": 1,
                "active_flag": True,
                "resource_profile": {},
                "created_at": "2026-05-07T00:00:00Z",
            },
        }

    def create_basin_with_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["basin_id"] == "dupe":
            raise DuplicateResourceError("basin_id already exists: dupe")
        return {
            "basin": {"basin_id": payload["basin_id"], "basin_name": payload["basin_name"]},
            "basin_version": {"basin_version_id": payload["basin_version"].get("basin_version_id") or "basin_v01"},
        }

    def create_basin_version(self, basin_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if basin_id == "missing":
            raise MissingResourceError("basin_id not found: missing")
        return {"basin_id": basin_id, "basin_version_id": payload.get("basin_version_id") or f"{basin_id}_v01"}

    def create_river_network(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["basin_version_id"] == "missing":
            raise InvalidReferenceError("basin_version_id does not exist: missing")
        return {"river_network_version": {"river_network_version_id": "basin_rivnet_v01"}, "segment_count": 1}

    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        assert basin_version_id == "basin_v01"
        assert river_network_version_id == "basin_rivnet_v01"
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "segment_id": "seg_001",
                        "river_segment_id": "seg_001",
                        "basin_version_id": "basin_v01",
                        "river_network_version_id": "basin_rivnet_v01",
                        "name": "Segment 001",
                        "stream_order": 2,
                    },
                    "geometry": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]},
                }
            ],
            "total": 1,
            "feature_total": 1,
            "limit": limit,
            "offset": offset,
        }

    def create_mesh_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["version_label"] == "":
            raise InvalidPayloadError("version_label must contain at least one alphanumeric character.")
        return {"mesh_version_id": payload.get("mesh_version_id") or "basin_mesh_v01"}

    def create_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["model_id"] == "registry_error":
            raise ModelRegistryError(
                "DATABASE_URL=postgresql://nhms:secret@localhost:5432/nhms failed in /srv/nhms/registry.py"
            )
        if payload["model_id"] == "unexpected_error":
            raise RuntimeError("psycopg OperationalError: password leaked in raw driver diagnostics")
        record = dict(payload)
        record.setdefault("active_flag", False)
        self.models[record["model_id"]] = record
        return record

    def set_model_active(self, model_id: str, active: bool) -> dict[str, Any]:
        if model_id not in self.models:
            raise MissingResourceError(f"model_id not found: {model_id}")
        model = self.models[model_id]
        if model["active_flag"] == active:
            raise DuplicateResourceError(f"model_id {model_id} is already active.")
        model["active_flag"] = active
        return dict(model)

    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        items = list(self.models.values())
        if basin_version_id is not None:
            items = [item for item in items if item["basin_version_id"] == basin_version_id]
        if active is not None:
            items = [item for item in items if item["active_flag"] == active]
        return {"total": len(items), "items": items[offset : offset + limit], "limit": limit, "offset": offset}

    def get_model(self, model_id: str) -> dict[str, Any]:
        if model_id not in self.models:
            raise MissingResourceError(f"model_id not found: {model_id}")
        return dict(self.models[model_id])

    def create_crosswalk_entries(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"count": len(payload["entries"]), "items": payload["entries"]}


class NullGeometryModelRegistryStore(FakeModelRegistryStore):
    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        assert basin_version_id == "basin_v01"
        assert river_network_version_id == "basin_rivnet_v01"
        return {
            "type": "FeatureCollection",
            "features": [],
            "total": 1,
            "feature_total": 0,
            "limit": limit,
            "offset": offset,
        }


class BasinsRiverSegmentStore(FakeModelRegistryStore):
    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        assert basin_version_id == "basins_basin_a_vbasins"
        assert river_network_version_id == "basins_basin_a_rivnet_vbasins"
        assert limit == 1
        assert offset == 0
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "segment_id": "basins_basin_a_shud_seg_1",
                        "river_segment_id": "basins_basin_a_shud_seg_1",
                        "basin_version_id": "basins_basin_a_vbasins",
                        "river_network_version_id": "basins_basin_a_rivnet_vbasins",
                        "basin_slug": "basin-a",
                        "shud_input_name": "alias-a",
                        "name": "Basins Segment 1",
                        "stream_order": 1,
                        "segment_order": 1,
                        "downstream_segment_id": "basins_basin_a_shud_seg_2",
                        "length_m": 1234.5,
                    },
                    "geometry": {"type": "LineString", "coordinates": [[90.0, 25.0], [90.5, 25.5]]},
                }
            ],
            "total": 2,
            "feature_total": 2,
            "limit": limit,
            "offset": offset,
        }


@pytest.fixture
def fake_store() -> FakeModelRegistryStore:
    store = FakeModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    return store


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_basin_rejects_duplicate(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/basins",
            json={
                "basin_id": "dupe",
                "basin_name": "Duplicate Basin",
                "basin_version": {
                    "version_label": "v01",
                    "geom": {
                        "type": "MultiPolygon",
                        "coordinates": [[[[90, 25], [91, 25], [91, 26], [90, 26], [90, 25]]]],
                    },
                },
            },
        )

    assert fake_store is not None
    assert response.status_code == 409
    _assert_error_envelope(
        response.json(),
        code="MODEL_REGISTRY_DUPLICATE",
        message_contains="already exists",
        error_type="DuplicateResourceError",
    )


@pytest.mark.asyncio
async def test_river_network_invalid_reference_returns_422(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/river-networks",
            json={
                "basin_version_id": "missing",
                "version_label": "v01",
                "segments": [
                    {
                        "river_segment_id": "seg_001",
                        "segment_order": 1,
                        "geom": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]},
                    }
                ],
            },
        )

    assert fake_store is not None
    assert response.status_code == 422
    _assert_error_envelope(
        response.json(),
        code="MODEL_REGISTRY_INVALID_REFERENCE",
        message_contains="basin_version_id",
        error_type="InvalidReferenceError",
    )


@pytest.mark.asyncio
async def test_list_river_segments_returns_backend_geojson(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments",
            params={"river_network_version_id": "basin_rivnet_v01", "limit": 100, "offset": 0},
        )

    assert fake_store is not None
    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["type"] == "FeatureCollection"
    expected_properties = {
        "segment_id": "seg_001",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "basin_rivnet_v01",
    }
    assert expected_properties.items() <= payload["features"][0]["properties"].items()
    assert payload["features"][0]["geometry"]["type"] == "LineString"
    assert payload["total"] == 1
    assert payload["feature_total"] == 1


@pytest.mark.asyncio
async def test_list_river_segments_can_omit_null_geometry_features() -> None:
    store = NullGeometryModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments",
            params={"river_network_version_id": "basin_rivnet_v01", "limit": 100, "offset": 0},
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["total"] == 1
    assert payload["feature_total"] == 0
    assert payload["features"] == []


@pytest.mark.asyncio
async def test_basins_river_segment_api_returns_paginated_geojson_for_map_rendering() -> None:
    store = BasinsRiverSegmentStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basins_basin_a_vbasins/river-segments",
            params={"river_network_version_id": "basins_basin_a_rivnet_vbasins", "limit": 1, "offset": 0},
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["type"] == "FeatureCollection"
    assert payload["total"] == 2
    assert payload["feature_total"] == 2
    assert payload["limit"] == 1
    feature = payload["features"][0]
    assert feature["geometry"]["type"] == "LineString"
    assert feature["properties"]["river_segment_id"] == "basins_basin_a_shud_seg_1"
    assert feature["properties"]["basin_version_id"] == "basins_basin_a_vbasins"
    assert feature["properties"]["river_network_version_id"] == "basins_basin_a_rivnet_vbasins"
    assert feature["properties"]["basin_slug"] == "basin-a"
    assert feature["properties"]["shud_input_name"] == "alias-a"


@pytest.mark.asyncio
async def test_model_listing_active_filter_vectors(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        default_response = await client.get("/api/v1/models")
        active_response_list = await client.get("/api/v1/models", params={"active": "true", "limit": 10, "offset": 0})
        inactive_response = await client.get("/api/v1/models", params={"active": "false", "limit": 10, "offset": 0})
        all_response = await client.get("/api/v1/models", params={"active": "all", "limit": 10, "offset": 0})

    assert fake_store is not None
    _assert_model_page(default_response.json(), expected_ids={"active_model"}, expected_limit=100)
    _assert_model_page(active_response_list.json(), expected_ids={"active_model"}, expected_limit=10)
    _assert_model_page(inactive_response.json(), expected_ids={"inactive_model"}, expected_limit=10)
    _assert_model_page(all_response.json(), expected_ids={"inactive_model", "active_model"}, expected_limit=10)


@pytest.mark.asyncio
async def test_active_toggle_uses_success_and_error_envelopes(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        active_response = await client.put("/api/v1/models/inactive_model/active", json={"active": True})
        conflict_response = await client.put("/api/v1/models/inactive_model/active", json={"active": True})

    assert fake_store.models["inactive_model"]["active_flag"] is True
    assert active_response.status_code == 200
    active_body = active_response.json()
    assert active_body["status"] == "ok"
    assert active_body["data"]["active_flag"] is True
    assert conflict_response.status_code == 409
    _assert_error_envelope(
        conflict_response.json(),
        code="MODEL_REGISTRY_DUPLICATE",
        message_contains="already active",
        error_type="DuplicateResourceError",
    )


@pytest.mark.asyncio
async def test_basins_inactive_model_listing_then_explicit_activation(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        default_before = await client.get("/api/v1/models")
        inactive_before = await client.get("/api/v1/models", params={"active": "false"})
        all_before = await client.get("/api/v1/models", params={"active": "all"})
        activation = await client.put("/api/v1/models/inactive_model/active", json={"active": True})
        default_after = await client.get("/api/v1/models")
        inactive_after = await client.get("/api/v1/models", params={"active": "false"})

    assert fake_store is not None
    assert default_before.status_code == 200
    assert inactive_before.status_code == 200
    assert all_before.status_code == 200
    assert activation.status_code == 200
    assert default_after.status_code == 200
    assert inactive_after.status_code == 200

    assert "inactive_model" not in _model_ids(default_before.json())
    assert "inactive_model" in _model_ids(inactive_before.json())
    assert "inactive_model" in _model_ids(all_before.json())
    assert "inactive_model" in _model_ids(default_after.json())
    assert "inactive_model" not in _model_ids(inactive_after.json())

    activated = activation.json()["data"]
    assert activated["active_flag"] is True
    assert activated["resource_profile"] == {
        "basin_slug": "basin-a",
        "shud_input_name": "basin_a",
        "manifest_uri": "s3://nhms/models/inactive_model/v1/manifest.json",
        "package_checksum": "package-sha-1",
        "source_inventory_checksum": "inventory-sha-1",
    }


@pytest.mark.asyncio
async def test_get_basins_model_detail_returns_asset_metadata(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/models/inactive_model")

    assert fake_store is not None
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    data = body["data"]
    assert {
        "model_id": "inactive_model",
        "model_name": "basin_a",
        "basin_id": "basins_basin_a",
        "basin_name": "Basin A",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "basin_rivnet_v01",
        "mesh_version_id": "basin_mesh_v01",
        "calibration_version_id": "basin_cal_v01",
        "segment_count": 2,
        "mesh_uri": "s3://nhms/models/inactive_model/v1/package/basin_a.sp.mesh",
        "mesh_checksum": "mesh-sha-1",
        "model_package_uri": "s3://nhms/models/inactive_model/package/",
        "package_checksum": "package-sha-1",
        "active_flag": False,
        "manifest_uri": "s3://nhms/models/inactive_model/v1/manifest.json",
        "source_inventory_checksum": "inventory-sha-1",
        "basin_slug": "basin-a",
        "shud_input_name": "basin_a",
    }.items() <= data.items()


@pytest.mark.asyncio
async def test_get_missing_model_detail_uses_not_found_envelope(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/models/missing_model")

    assert fake_store is not None
    assert response.status_code == 404
    _assert_error_envelope(
        response.json(),
        code="MODEL_REGISTRY_NOT_FOUND",
        message_contains="missing_model",
        error_type="MissingResourceError",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_method", "path", "payload", "expected_status", "expected_code", "message_contains", "error_type"),
    [
        (
            "put",
            "/api/v1/models/missing_model/active",
            {"active": True},
            404,
            "MODEL_REGISTRY_NOT_FOUND",
            "missing_model",
            "MissingResourceError",
        ),
        (
            "post",
            "/api/v1/mesh-versions",
            {
                "basin_version_id": "basin_v01",
                "version_label": "",
                "mesh_uri": "s3://nhms/mesh",
            },
            422,
            "MODEL_REGISTRY_INVALID_PAYLOAD",
            "version_label",
            "InvalidPayloadError",
        ),
        (
            "post",
            "/api/v1/models",
            {
                "model_id": "registry_error",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/registry_error/package/",
            },
            500,
            "MODEL_REGISTRY_ERROR",
            "Model registry operation failed.",
            "ModelRegistryError",
        ),
        (
            "post",
            "/api/v1/models",
            {
                "model_id": "unexpected_error",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/unexpected_error/package/",
            },
            500,
            "MODEL_REGISTRY_ERROR",
            "Model registry operation failed.",
            "RuntimeError",
        ),
    ],
)
async def test_model_registry_error_envelope_vectors(
    fake_store: FakeModelRegistryStore,
    caplog: pytest.LogCaptureFixture,
    request_method: str,
    path: str,
    payload: dict[str, Any],
    expected_status: int,
    expected_code: str,
    message_contains: str,
    error_type: str,
) -> None:
    caplog.set_level(logging.ERROR, logger="apps.api.routes.models")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await getattr(client, request_method)(path, json=payload)

    assert fake_store is not None
    assert response.status_code == expected_status
    _assert_error_envelope(
        response.json(),
        code=expected_code,
        message_contains=message_contains,
        error_type=error_type,
    )
    if expected_status == 500:
        rendered = str(response.json())
        log_records = [record for record in caplog.records if record.name == "apps.api.routes.models"]
        rendered_logs = "\n".join(
            f"{record.getMessage()} {getattr(record, 'error_type', '')} {record.exc_text or ''}"
            for record in log_records
        )
        assert log_records
        assert all(record.exc_info is None for record in log_records)
        assert error_type in rendered_logs
        for unsafe in (
            "DATABASE_URL",
            "postgresql://",
            "nhms:secret",
            "/srv/nhms/registry.py",
            "OperationalError",
            "driver diagnostics",
            "password leaked",
        ):
            assert unsafe not in rendered
            assert unsafe not in rendered_logs


@pytest.mark.asyncio
async def test_model_package_validation_error_uses_error_envelope(
    fake_store: FakeModelRegistryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models",
            json={
                "model_id": "bad_package",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/missing/package/",
            },
        )

    assert fake_store is not None
    assert response.status_code == 422
    _assert_error_envelope(
        response.json(),
        code="MODEL_PACKAGE_VALIDATION_ERROR",
        message_contains="model_package_uri",
        error_type="ModelPackageValidationError",
    )


def test_geometry_to_wkt_accepts_expected_geojson_shapes() -> None:
    assert geometry_to_wkt({"type": "LineString", "coordinates": [[90, 25], [91, 26]]}, "LineString") == (
        "LINESTRING(90 25, 91 26)"
    )
    assert geometry_to_wkt(
        {"type": "MultiPolygon", "coordinates": [[[[90, 25], [91, 25], [91, 26], [90, 26], [90, 25]]]]},
        "MultiPolygon",
    ).startswith("MULTIPOLYGON")


def test_model_package_validator_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    package = tmp_path / "model_package"
    package.mkdir()
    for suffix in ("mesh", "para", "calib"):
        (package / f"basin.{suffix}").write_text("x\n", encoding="utf-8")

    result = validate_model_package_path(package)
    exit_code = _argparse_main(["validate-package", str(package)])

    assert result.passed is True
    assert exit_code == 0
    assert "All required model package files are present" in capsys.readouterr().out


def test_legacy_model_package_validator_entrypoints_still_accept_local_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    object_root = tmp_path / "object-store"
    package = object_root / "models" / "demo" / "package"
    package.mkdir(parents=True)
    for suffix in ("mesh", "para", "calib"):
        (package / f"basin.{suffix}").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    path_result = validate_model_package_path(package)
    uri_result = validate_model_package_uri("s3://nhms/models/demo/package")
    exit_code = _argparse_main(["validate-package", str(package)])

    assert path_result.passed is True
    assert path_result.package_path == str(package)
    assert uri_result is not None
    assert uri_result.passed is True
    assert uri_result.package_path == str(package)
    assert set(uri_result.matched_files) == {"basin.mesh", "basin.para", "basin.calib"}
    assert exit_code == 0
    assert "All required model package files are present" in capsys.readouterr().out


def test_model_package_validator_reports_missing_files(tmp_path: Path) -> None:
    package = tmp_path / "bad_package"
    package.mkdir()
    (package / "basin.mesh").write_text("x\n", encoding="utf-8")

    with pytest.raises(ModelPackageValidationError, match=r"\*\.para"):
        validate_model_package_path(package)


def test_model_package_uri_rejects_paths_outside_object_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    with pytest.raises(ModelPackageValidationError) as exc_info:
        validate_model_package_uri("s3://nhms/../outside/package")

    assert "escapes object store root" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)


def test_model_package_uri_missing_package_error_redacts_local_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")

    with pytest.raises(ModelPackageValidationError) as exc_info:
        validate_model_package_uri("s3://nhms/models/missing/package")

    assert "model_package_uri" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)


def test_create_model_rejects_mesh_version_from_different_basin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any] | None] = [
                {"exists": 1},
                {"basin_version_id": "basin_v01"},
                {"basin_version_id": "basin_v02"},
            ]
            self.statements: list[str] = []

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            self.statements.append(statement)

        def fetchone(self) -> dict[str, Any] | None:
            return self.rows.pop(0)

    class FakeTransaction:
        def __init__(self, cursor: FakeCursor) -> None:
            self.cursor = cursor

        def __enter__(self) -> FakeCursor:
            return self.cursor

        def __exit__(self, *_args: object) -> bool:
            return False

    cursor = FakeCursor()
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction(cursor))
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(InvalidReferenceError, match="mesh_version_id"):
        store.create_model(
            {
                "model_id": "demo_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "rivnet_v01",
                "mesh_version_id": "mesh_v02",
                "calibration_version_id": "cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/demo/package/",
            }
        )

    assert not any("INSERT INTO core.model_instance" in statement for statement in cursor.statements)


def test_set_model_active_writes_audit_details_after_successful_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = [
                {
                    "model_id": "basins_model",
                    "basin_version_id": "basin_v01",
                    "river_network_version_id": "basin_rivnet_v01",
                    "mesh_version_id": "basin_mesh_v01",
                    "model_package_uri": (
                        "s3://user:pass@nhms/models/basins_model/package/?token=secret#credential"
                    ),
                    "active_flag": False,
                    "resource_profile": {
                        "basin_slug": "basin-a",
                        "shud_input_name": "basin_a",
                        "manifest_uri": (
                            "s3://user:pass@nhms/models/basins_model/v1/manifest.json?token=secret#credential"
                        ),
                        "package_checksum": "package-sha-1",
                        "source_inventory_checksum": "inventory-sha-1",
                        "other": "kept-out-of-audit-lineage",
                    },
                },
                {
                    "model_id": "basins_model",
                    "basin_version_id": "basin_v01",
                    "river_network_version_id": "basin_rivnet_v01",
                    "mesh_version_id": "basin_mesh_v01",
                    "model_package_uri": (
                        "s3://user:pass@nhms/models/basins_model/package/?token=secret#credential"
                    ),
                    "active_flag": True,
                    "resource_profile": {
                        "basin_slug": "basin-a",
                        "shud_input_name": "basin_a",
                        "manifest_uri": (
                            "s3://user:pass@nhms/models/basins_model/v1/manifest.json?token=secret#credential"
                        ),
                        "package_checksum": "package-sha-1",
                        "source_inventory_checksum": "inventory-sha-1",
                        "other": "kept-out-of-audit-lineage",
                    },
                },
            ]
            self.statements: list[str] = []
            self.parameters: list[tuple[Any, ...]] = []

        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.statements.append(statement)
            self.parameters.append(parameters)

        def fetchone(self) -> dict[str, Any]:
            return self.rows.pop(0)

    class FakeTransaction:
        def __init__(self, cursor: FakeCursor) -> None:
            self.cursor = cursor

        def __enter__(self) -> FakeCursor:
            return self.cursor

        def __exit__(self, *_args: object) -> bool:
            return False

    cursor = FakeCursor()
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction(cursor))
    monkeypatch.setattr(PsycopgModelRegistryStore, "_json", lambda _self, value: dict(value))
    store = PsycopgModelRegistryStore("postgresql://example")

    result = store.set_model_active("basins_model", True)

    assert result["active_flag"] is True
    assert result["model_package_uri"] == "s3://nhms/models/basins_model/package/"
    assert result["resource_profile"]["manifest_uri"] == "s3://nhms/models/basins_model/v1/manifest.json"
    assert sum("INSERT INTO ops.audit_log" in statement for statement in cursor.statements) == 1
    audit_parameters = cursor.parameters[-1]
    assert audit_parameters[:3] == ("nhms-api", "model-registry", "basins_model")
    details = audit_parameters[3]
    assert "other" not in str(details)
    assert "token=" not in str(details)
    assert "user:pass@" not in str(details)
    assert "?" not in details["model_package_uri"]
    assert "#" not in details["model_package_uri"]
    assert details == {
        "previous_active": False,
        "active": True,
        "basin_version_id": "basin_v01",
        "river_network_version_id": "basin_rivnet_v01",
        "mesh_version_id": "basin_mesh_v01",
        "model_package_uri": "s3://nhms/models/basins_model/package/",
        "basins_lineage": {
            "basin_slug": "basin-a",
            "shud_input_name": "basin_a",
            "manifest_uri": "s3://nhms/models/basins_model/v1/manifest.json",
            "package_checksum": "package-sha-1",
            "source_inventory_checksum": "inventory-sha-1",
        },
    }


def test_set_model_active_duplicate_and_missing_do_not_write_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self.row = row
            self.statements: list[str] = []

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            self.statements.append(statement)

        def fetchone(self) -> dict[str, Any] | None:
            return self.row

    class FakeTransaction:
        def __init__(self, cursor: FakeCursor) -> None:
            self.cursor = cursor

        def __enter__(self) -> FakeCursor:
            return self.cursor

        def __exit__(self, *_args: object) -> bool:
            return False

    duplicate_cursor = FakeCursor({"active_flag": True})
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction(duplicate_cursor))
    store = PsycopgModelRegistryStore("postgresql://example")
    with pytest.raises(DuplicateResourceError):
        store.set_model_active("basins_model", True)
    assert not any("INSERT INTO ops.audit_log" in statement for statement in duplicate_cursor.statements)

    missing_cursor = FakeCursor(None)
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction(missing_cursor))
    with pytest.raises(MissingResourceError):
        store.set_model_active("missing_model", True)
    assert not any("INSERT INTO ops.audit_log" in statement for statement in missing_cursor.statements)


def test_get_model_joins_asset_metadata_and_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            self.statements.append(statement)

        def fetchone(self) -> dict[str, Any]:
            return {
                "model_id": "basins_basin_a_shud",
                "basin_version_id": "basins_basin_a_vbasins",
                "river_network_version_id": "basins_basin_a_rivnet_vbasins",
                "mesh_version_id": "basins_basin_a_mesh_vbasins",
                "calibration_version_id": "basins_basin_a_shud_calib_vbasins",
                "shud_code_version": "basins-shud",
                "rshud_code_version": None,
                "autoshud_code_version": None,
                "container_image": None,
                "model_package_uri": "s3://nhms/models/basins_basin_a_shud/vbasins/package/",
                "active_flag": False,
                "resource_profile": {
                    "basin_slug": "basin-a",
                    "shud_input_name": "alias-a",
                    "manifest_uri": (
                        "s3://user:pass@nhms/models/basins_basin_a_shud/vbasins/manifest.json?token=secret#frag"
                    ),
                    "package_checksum": "package-sha-1",
                    "source_inventory_checksum": "inventory-sha-1",
                    "source_path": "//user:pass@nhms/source-path?token=secret#frag",
                    "resolved_source_path": "//user:pass@nhms/resolved-source-path?token=secret#frag",
                    "source_uri": "s3://user:pass@nhms/sources/basin-a?token=secret#frag",
                    "source_is_symlink": False,
                    "source_lineage": {
                        "uris": [
                            "s3://user:pass@nhms/sources/nested?token=secret#frag",
                            "//user:pass@nhms/protocol-relative?token=secret#frag",
                            "/volume/data/nwm/Basins/local-source",
                        ],
                        "label": "s3 path label, not a URI",
                        "local_path": "/volume/data/nwm/Basins/ordinary",
                    },
                },
                "created_at": "2026-05-14T00:00:00Z",
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "segment_count": 2,
                "mesh_uri": "s3://user:pass@nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh?token=secret#frag",
                "mesh_checksum": "mesh-sha-1",
                "mesh_properties_json": {
                    "manifest_uri": "s3://nhms/models/basins_basin_a_shud/vbasins/manifest-from-mesh.json",
                    "source_path": "s3://user:pass@nhms/source-path-fallback?token=secret#frag",
                    "resolved_source_path": "s3://user:pass@nhms/resolved-source-path-fallback?token=secret#frag",
                },
            }

    class FakeTransaction:
        def __init__(self, cursor: FakeCursor) -> None:
            self.cursor = cursor

        def __enter__(self) -> FakeCursor:
            return self.cursor

        def __exit__(self, *_args: object) -> bool:
            return False

    cursor = FakeCursor()
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction(cursor))
    store = PsycopgModelRegistryStore("postgresql://example")

    detail = store.get_model("basins_basin_a_shud")

    assert "JOIN core.basin b" in cursor.statements[0]
    assert "JOIN core.river_network_version rnv" in cursor.statements[0]
    assert "LEFT JOIN core.mesh_version mv" in cursor.statements[0]
    assert detail["model_id"] == "basins_basin_a_shud"
    assert detail["model_name"] == "alias-a"
    assert detail["basin_id"] == "basins_basin_a"
    assert detail["basin_name"] == "Basin A"
    assert detail["segment_count"] == 2
    assert detail["mesh_uri"] == "s3://nhms/models/basins_basin_a_shud/vbasins/package/alias-a.sp.mesh"
    assert detail["mesh_checksum"] == "mesh-sha-1"
    assert detail["package_checksum"] == "package-sha-1"
    assert detail["manifest_uri"] == "s3://nhms/models/basins_basin_a_shud/vbasins/manifest.json"
    assert detail["source_inventory_checksum"] == "inventory-sha-1"
    assert detail["basin_slug"] == "basin-a"
    assert detail["shud_input_name"] == "alias-a"
    assert detail["source_path"] == "//nhms/source-path"
    assert detail["resolved_source_path"] == "//nhms/resolved-source-path"
    assert detail["source_uri"] == "s3://nhms/sources/basin-a"
    assert detail["source_is_symlink"] is False
    assert detail["resource_profile"]["manifest_uri"] == (
        "s3://nhms/models/basins_basin_a_shud/vbasins/manifest.json"
    )
    assert detail["resource_profile"]["source_uri"] == "s3://nhms/sources/basin-a"
    assert detail["resource_profile"]["source_lineage"]["uris"] == [
        "s3://nhms/sources/nested",
        "//nhms/protocol-relative",
        "/volume/data/nwm/Basins/local-source",
    ]
    assert detail["resource_profile"]["source_lineage"]["label"] == "s3 path label, not a URI"
    assert detail["resource_profile"]["source_lineage"]["local_path"] == "/volume/data/nwm/Basins/ordinary"
    public_profile_json = json.dumps(detail["resource_profile"])
    assert "token=secret" not in public_profile_json
    assert "user:pass@" not in public_profile_json
    assert "#frag" not in public_profile_json
    assert "mesh_properties_json" not in detail


def test_get_model_uses_sanitized_mesh_lineage_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def execute(self, _statement: str, _parameters: tuple[Any, ...]) -> None:
            return None

        def fetchone(self) -> dict[str, Any]:
            return {
                "model_id": "basins_basin_a_shud",
                "basin_version_id": "basins_basin_a_vbasins",
                "river_network_version_id": "basins_basin_a_rivnet_vbasins",
                "mesh_version_id": "basins_basin_a_mesh_vbasins",
                "calibration_version_id": "basins_basin_a_shud_calib_vbasins",
                "shud_code_version": "basins-shud",
                "rshud_code_version": None,
                "autoshud_code_version": None,
                "container_image": None,
                "model_package_uri": "s3://user:pass@nhms/models/basins_basin_a_shud/vbasins/package/?token=secret#frag",
                "active_flag": False,
                "resource_profile": {},
                "created_at": "2026-05-14T00:00:00Z",
                "basin_id": "basins_basin_a",
                "basin_name": "Basin A",
                "segment_count": 2,
                "mesh_uri": None,
                "mesh_checksum": None,
                "mesh_properties_json": {
                    "manifest_uri": "s3://user:pass@nhms/models/basins_basin_a_shud/vbasins/manifest.json?token=secret#frag",
                    "source_path": "s3://user:pass@nhms/source-path?token=secret#frag",
                    "resolved_source_path": "s3://user:pass@nhms/resolved-source-path?token=secret#frag",
                    "source_uri": "s3://user:pass@nhms/source-uri?token=secret#frag",
                    "source_is_symlink": True,
                },
            }

    class FakeTransaction:
        def __enter__(self) -> FakeCursor:
            return FakeCursor()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    detail = store.get_model("basins_basin_a_shud")

    assert detail["model_package_uri"] == "s3://nhms/models/basins_basin_a_shud/vbasins/package/"
    assert detail["manifest_uri"] == "s3://nhms/models/basins_basin_a_shud/vbasins/manifest.json"
    assert detail["source_path"] == "s3://nhms/source-path"
    assert detail["resolved_source_path"] == "s3://nhms/resolved-source-path"
    assert detail["source_uri"] == "s3://nhms/source-uri"
    assert detail["source_is_symlink"] is True


def test_list_models_returns_public_safe_resource_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self._result: Any = None

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            if "SELECT COUNT" in statement:
                self._result = {"total": 1}
            else:
                self._result = [
                    {
                        "model_id": "basins_basin_a_shud",
                        "basin_version_id": "basins_basin_a_vbasins",
                        "river_network_version_id": "basins_basin_a_rivnet_vbasins",
                        "mesh_version_id": "basins_basin_a_mesh_vbasins",
                        "calibration_version_id": "basins_basin_a_shud_calib_vbasins",
                        "shud_code_version": "basins-shud",
                        "model_package_uri": (
                            "s3://user:pass@nhms/models/basins_basin_a_shud/package/?token=secret#frag"
                        ),
                        "active_flag": False,
                        "resource_profile": {
                            "manifest_uri": (
                                "//user:pass@nhms/models/basins_basin_a_shud/manifest.json?token=secret#frag"
                            ),
                            "source_uri": "s3://user:pass@nhms/sources/basin-a?token=secret#frag",
                            "source_path": "/volume/data/nwm/Basins/basin-a",
                            "resolved_source_path": "//user:pass@nhms/resolved-source-path?token=secret#frag",
                            "nested": [
                                {"uri": "s3://user:pass@nhms/nested?token=secret#frag"},
                                {"uri": "//user:pass@nhms/nested-protocol-relative?token=secret#frag"},
                                "normal string",
                            ],
                        },
                        "created_at": "2026-05-14T00:00:00Z",
                    }
                ]

        def fetchone(self) -> dict[str, Any]:
            return self._result

        def fetchall(self) -> list[dict[str, Any]]:
            return self._result

    class FakeTransaction:
        def __enter__(self) -> FakeCursor:
            return FakeCursor()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    page = store.list_models(basin_version_id=None, active=None, limit=10, offset=0)

    item = page["items"][0]
    assert item["model_package_uri"] == "s3://nhms/models/basins_basin_a_shud/package/"
    assert item["resource_profile"]["manifest_uri"] == "//nhms/models/basins_basin_a_shud/manifest.json"
    assert item["resource_profile"]["source_uri"] == "s3://nhms/sources/basin-a"
    assert item["resource_profile"]["source_path"] == "/volume/data/nwm/Basins/basin-a"
    assert item["resource_profile"]["resolved_source_path"] == "//nhms/resolved-source-path"
    assert item["resource_profile"]["nested"] == [
        {"uri": "s3://nhms/nested"},
        {"uri": "//nhms/nested-protocol-relative"},
        "normal string",
    ]
    public_item_json = json.dumps(item)
    assert "token=secret" not in public_item_json
    assert "user:pass@" not in public_item_json
    assert "#frag" not in public_item_json


def _assert_model_page(body: dict[str, Any], *, expected_ids: set[str], expected_limit: int) -> None:
    assert set(body) == {"request_id", "status", "data"}
    assert body["request_id"]
    assert body["status"] == "ok"
    data = body["data"]
    assert set(data) == {"total", "items", "limit", "offset"}
    assert data["total"] == len(expected_ids)
    assert data["limit"] == expected_limit
    assert data["offset"] == 0
    assert {item["model_id"] for item in data["items"]} == expected_ids


def _model_ids(body: dict[str, Any]) -> set[str]:
    return {item["model_id"] for item in body["data"]["items"]}


def _assert_error_envelope(
    body: dict[str, Any],
    *,
    code: str,
    message_contains: str,
    error_type: str,
) -> None:
    assert set(body) == {"request_id", "status", "error"}
    assert body["request_id"]
    assert body["status"] == "error"
    assert body["error"]["code"] == code
    assert message_contains in body["error"]["message"]
    assert body["error"]["details"] == {"error_type": error_type}
