from __future__ import annotations

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
        if basin_id == "missing":
            raise MissingResourceError("basin_id not found: missing")
        return {"basin_id": basin_id, "basin_version_id": payload.get("basin_version_id") or f"{basin_id}_v01"}

    def create_river_network(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["basin_version_id"] == "missing":
            raise InvalidReferenceError("basin_version_id does not exist: missing")
        return {"river_network_version": {"river_network_version_id": "basin_rivnet_v01"}, "segment_count": 1}

    def create_mesh_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["version_label"] == "":
            raise InvalidPayloadError("version_label must contain at least one alphanumeric character.")
        return {"mesh_version_id": payload.get("mesh_version_id") or "basin_mesh_v01"}

    def create_model(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload["model_id"] == "registry_error":
            raise ModelRegistryError("DATABASE_URL is required for model registry operations.")
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
            "DATABASE_URL",
            "ModelRegistryError",
        ),
    ],
)
async def test_model_registry_error_envelope_vectors(
    fake_store: FakeModelRegistryStore,
    request_method: str,
    path: str,
    payload: dict[str, Any],
    expected_status: int,
    expected_code: str,
    message_contains: str,
    error_type: str,
) -> None:
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
