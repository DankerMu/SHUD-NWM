from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.main import app
from apps.api.routes.models import get_model_registry_store
from packages.common.model_registry import DuplicateResourceError, InvalidReferenceError, geometry_to_wkt
from workers.model_registry.cli import _argparse_main
from workers.model_registry.validator import ModelPackageValidationError, validate_model_package_path


class FakeModelRegistryStore:
    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {
            "inactive_model": {
                "model_id": "inactive_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/inactive_model/package/",
                "active_flag": False,
                "resource_profile": {},
                "created_at": "2026-05-07T00:00:00Z",
            },
            "active_model": {
                "model_id": "active_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/active_model/package/",
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
        return {"basin_id": basin_id, "basin_version_id": payload.get("basin_version_id") or f"{basin_id}_v01"}

    def create_river_network(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["basin_version_id"] == "missing":
            raise InvalidReferenceError("basin_version_id does not exist: missing")
        return {"river_network_version": {"river_network_version_id": "basin_rivnet_v01"}, "segment_count": 1}

    def create_mesh_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"mesh_version_id": payload.get("mesh_version_id") or "basin_mesh_v01"}

    def create_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload)
        record.setdefault("active_flag", False)
        self.models[record["model_id"]] = record
        return record

    def set_model_active(self, model_id: str, active: bool) -> dict[str, Any]:
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

    def create_crosswalk_entries(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"count": len(payload["entries"]), "items": payload["entries"]}


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
    assert "already exists" in response.json()["detail"]


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
    assert "basin_version_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_active_toggle_and_default_model_listing(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        active_response = await client.put("/api/v1/models/inactive_model/active", json={"active": True})
        conflict_response = await client.put("/api/v1/models/inactive_model/active", json={"active": True})
        list_response = await client.get("/api/v1/models")

    assert fake_store.models["inactive_model"]["active_flag"] is True
    assert active_response.status_code == 200
    assert conflict_response.status_code == 409
    assert list_response.status_code == 200
    assert {item["model_id"] for item in list_response.json()["items"]} == {"inactive_model", "active_model"}


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


def test_model_package_validator_reports_missing_files(tmp_path: Path) -> None:
    package = tmp_path / "bad_package"
    package.mkdir()
    (package / "basin.mesh").write_text("x\n", encoding="utf-8")

    with pytest.raises(ModelPackageValidationError, match=r"\*\.para"):
        validate_model_package_path(package)
