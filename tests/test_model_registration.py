from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope

from apps.api.auth import AuthContext, evaluate_policy
from apps.api.main import app
from apps.api.routes.models import get_model_registry_store
from packages.common.model_registry import (
    RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES,
    RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
    RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES,
    DuplicateResourceError,
    InvalidPayloadError,
    InvalidReferenceError,
    MissingResourceError,
    ModelRegistryError,
    PsycopgModelRegistryStore,
    RiverSegmentGeoJsonBudgetError,
    _is_unsafe_source_value,
    geometry_to_wkt,
    sanitize_model_detail_payload,
    sanitize_model_list_payload,
)
from workers.model_registry.cli import _argparse_main
from workers.model_registry.validator import (
    ModelPackageValidationError,
    validate_model_package_path,
    validate_model_package_uri,
)


class FakeModelRegistryStore:
    def __init__(self) -> None:
        self.write_calls: list[str] = []
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
                "lifecycle_state": "inactive",
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
                "lifecycle_state": "active",
                "resource_profile": {},
                "created_at": "2026-05-07T00:00:00Z",
            },
        }
        self.activation_audit_rows: list[dict[str, Any]] = []
        self.lifecycle_calls: list[tuple[str, str]] = []
        self.preflight_calls: list[dict[str, Any]] = []

    def create_basin_with_version(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.write_calls.append("create_basin_with_version")
        if payload["basin_id"] == "dupe":
            raise DuplicateResourceError("basin_id already exists: dupe")
        return {
            "basin": {"basin_id": payload["basin_id"], "basin_name": payload["basin_name"]},
            "basin_version": {"basin_version_id": payload["basin_version"].get("basin_version_id") or "basin_v01"},
        }

    def create_basin_version(self, basin_id: str, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.write_calls.append("create_basin_version")
        if basin_id == "missing":
            raise MissingResourceError("basin_id not found: missing")
        return {"basin_id": basin_id, "basin_version_id": payload.get("basin_version_id") or f"{basin_id}_v01"}

    def create_river_network(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.write_calls.append("create_river_network")
        if payload["basin_version_id"] == "missing":
            raise InvalidReferenceError("basin_version_id does not exist: missing")
        return {"river_network_version": {"river_network_version_id": "basin_rivnet_v01"}, "segment_count": 1}

    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        search: str | None = None,
        stream_order_min: int | None = None,
        stream_order_max: int | None = None,
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

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        if (
            basin_version_id != "basin_v01"
            or river_network_version_id != "basin_rivnet_v01"
            or segment_id != "seg_001"
        ):
            raise MissingResourceError(
                "river_segment_id not found with renderable geometry for "
                f"basin_version_id {basin_version_id}, "
                f"river_network_version_id {river_network_version_id}: {segment_id}"
            )
        return {
            "river_segment_id": "seg_001",
            "river_network_version_id": "basin_rivnet_v01",
            "segment_order": 2,
            "downstream_segment_id": None,
            "length_m": 1234.5,
            "geom": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]},
            "properties_json": {
                "segment_id": "seg_001",
                "name": "Segment 001",
                "stream_order": 2,
            },
            "created_at": "2026-05-07T00:00:00Z",
        }

    def create_mesh_version(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.write_calls.append("create_mesh_version")
        if payload["version_label"] == "":
            raise InvalidPayloadError("version_label must contain at least one alphanumeric character.")
        return {"mesh_version_id": payload.get("mesh_version_id") or "basin_mesh_v01"}

    def create_model(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.write_calls.append("create_model")
        if payload["model_id"] == "registry_error":
            raise ModelRegistryError(
                "DATABASE_URL=postgresql://nhms:secret@localhost:5432/nhms failed in /srv/nhms/registry.py"
            )
        if payload["model_id"] == "unexpected_error":
            raise RuntimeError("psycopg OperationalError: password leaked in raw driver diagnostics")
        record = dict(payload)
        record.setdefault("active_flag", False)
        record.setdefault("lifecycle_state", "active" if record["active_flag"] else "inactive")
        self.models[record["model_id"]] = record
        return record

    def set_model_active(
        self,
        model_id: str,
        active: bool,
        *,
        policy_decision: Any = None,
        request_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.write_calls.append("set_model_active")
        if model_id not in self.models:
            raise MissingResourceError(f"model_id not found: {model_id}")
        model = self.models[model_id]
        if model["active_flag"] == active:
            raise DuplicateResourceError(f"model_id {model_id} is already active.")
        previous_active = bool(model["active_flag"])
        model["active_flag"] = active
        model["lifecycle_state"] = "active" if active else "inactive"
        self.activation_audit_rows.append(
            {
                "action": "models.activate" if active else "models.deactivate",
                "entity_type": "model_instance",
                "entity_id": model_id,
                "details": {
                    "request_id": request_id,
                    "actor": getattr(policy_decision, "actor_id", None),
                    "roles": list(getattr(policy_decision, "roles", ())),
                    "action_id": getattr(policy_decision, "action_id", None),
                    "decision": getattr(policy_decision, "decision", None),
                    "previous_active": previous_active,
                    "active": bool(active),
                },
            }
        )
        return dict(model)

    def preflight_model_operation(
        self,
        model_id: str,
        *,
        operation: str,
        policy_decision: Any = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self.preflight_calls.append(
            {
                "model_id": model_id,
                "operation": operation,
                "policy_decision": policy_decision,
                "previous_model_id": previous_model_id,
                "override_missing_active": override_missing_active,
                "reason": reason,
                "request_id": request_id,
            }
        )
        if model_id not in self.models:
            raise MissingResourceError(f"model_id not found: {model_id}")
        model = self.models[model_id]
        blockers = []
        if operation == "activate" and model.get("model_package_uri") == "ftp://unsafe/package":
            blockers.append(
                {"code": "OBJECT_URI_PREFIX_INVALID", "message": "Model package URI prefix is not supported."}
            )
        roles = tuple(getattr(policy_decision, "roles", ()))
        if operation == "deactivate" and model.get("active_flag") and not override_missing_active:
            blockers.append(
                {
                    "code": "MISSING_ACTIVE_RISK",
                    "message": "Operation would leave this basin version without an active model.",
                }
            )
        if operation == "deactivate" and override_missing_active and roles and "sys_admin" not in roles:
            blockers.append(
                {"code": "OVERRIDE_REQUIRES_SYS_ADMIN", "message": "Missing-active override requires sys_admin."}
            )
        return {
            "schema": "nhms.model_operation_preflight.v1",
            "request_id": request_id,
            "operation": operation,
            "status": "blocked" if blockers else "ready",
            "model_id": model_id,
            "basin_id": model.get("basin_id"),
            "basin_version_id": model.get("basin_version_id"),
            "current_active_model_id": "active_model",
            "river_network_version_id": model.get("river_network_version_id"),
            "mesh_version_id": model.get("mesh_version_id"),
            "impact": {
                "downstream_surfaces": ["forecast-routing"],
                "active_scope": {"basin_version_id": model.get("basin_version_id")},
            },
            "blockers": blockers,
            "warnings": [],
            "reason": "[redacted]",
        }

    def model_lifecycle_operation(
        self,
        model_id: str,
        *,
        operation: str,
        policy_decision: Any = None,
        request_id: str | None = None,
        previous_model_id: str | None = None,
        override_missing_active: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.lifecycle_calls.append((model_id, operation))
        preflight = self.preflight_model_operation(
            model_id,
            operation=operation,
            policy_decision=policy_decision,
            previous_model_id=previous_model_id,
            override_missing_active=override_missing_active,
            reason=reason,
            request_id=request_id,
        )
        model = self.models[model_id]
        if preflight["status"] == "blocked":
            self.activation_audit_rows.append(
                {
                    "action": getattr(policy_decision, "action_id", None),
                    "details": {"outcome": "blocked", "preflight": preflight},
                }
            )
            return {
                "status": "blocked",
                "operation": operation,
                "model": dict(model),
                "preflight": preflight,
                "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": 1},
            }
        if operation in {"activate", "switch_version"}:
            for item in self.models.values():
                if item.get("basin_version_id") == model.get("basin_version_id") and item.get("active_flag"):
                    item["active_flag"] = False
                    item["lifecycle_state"] = "superseded"
            model["active_flag"] = True
            model["lifecycle_state"] = "active"
        elif operation == "deactivate":
            model["active_flag"] = False
            model["lifecycle_state"] = "inactive"
        elif operation == "supersede":
            model["active_flag"] = False
            model["lifecycle_state"] = "superseded"
        elif operation == "deprecate":
            model["active_flag"] = False
            model["lifecycle_state"] = "deprecated"
        return {
            "status": "allowed",
            "operation": operation,
            "model": dict(model),
            "preflight": preflight,
            "audit_reference": {"entity_type": "model_instance", "entity_id": model_id, "log_id": 1},
        }

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

    def create_crosswalk_entries(self, payload: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.write_calls.append("create_crosswalk_entries")
        return {"count": len(payload["entries"]), "items": payload["entries"]}


class NullGeometryModelRegistryStore(FakeModelRegistryStore):
    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        search: str | None = None,
        stream_order_min: int | None = None,
        stream_order_max: int | None = None,
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

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        raise MissingResourceError(
            "river_segment_id not found with renderable geometry for "
            f"basin_version_id {basin_version_id}, "
            f"river_network_version_id {river_network_version_id}: {segment_id}"
        )


class OversizedGeometryModelRegistryStore(FakeModelRegistryStore):
    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        raise MissingResourceError(
            "river_segment_id not found with renderable geometry for "
            f"basin_version_id {basin_version_id}, "
            f"river_network_version_id {river_network_version_id}: {segment_id}"
        )


class DuplicateSegmentIdModelRegistryStore(FakeModelRegistryStore):
    segment_rows: dict[str, dict[str, Any]] = {
        "rivnet_old": {
            "river_segment_id": "seg_shared",
            "river_network_version_id": "rivnet_old",
            "segment_order": 1,
            "downstream_segment_id": None,
            "length_m": 100.0,
            "geom": {"type": "LineString", "coordinates": [[90.0, 25.0], [90.5, 25.5]]},
            "properties_json": {"name": "Old sibling network row"},
            "created_at": "2026-05-07T00:00:00Z",
        },
        "rivnet_selected": {
            "river_segment_id": "seg_shared",
            "river_network_version_id": "rivnet_selected",
            "segment_order": 2,
            "downstream_segment_id": "seg_downstream",
            "length_m": 200.0,
            "geom": {"type": "LineString", "coordinates": [[91.0, 26.0], [91.5, 26.5]]},
            "properties_json": {"name": "Selected sibling network row"},
            "created_at": "2026-05-08T00:00:00Z",
        },
    }

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        if basin_version_id != "basin_v01" or segment_id != "seg_shared":
            raise MissingResourceError(
                "river_segment_id not found with renderable geometry for "
                f"basin_version_id {basin_version_id}, "
                f"river_network_version_id {river_network_version_id}: {segment_id}"
            )
        row = self.segment_rows.get(river_network_version_id)
        if row is None:
            raise MissingResourceError(
                "river_segment_id not found with renderable geometry for "
                f"basin_version_id {basin_version_id}, "
                f"river_network_version_id {river_network_version_id}: {segment_id}"
            )
        return dict(row)


class OversizedPropertiesModelRegistryStore(FakeModelRegistryStore):
    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        search: str | None = None,
        stream_order_min: int | None = None,
        stream_order_max: int | None = None,
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
                        "segment_id": "seg_oversized",
                        "river_segment_id": "seg_oversized",
                        "basin_version_id": "basin_v01",
                        "river_network_version_id": "basin_rivnet_v01",
                        "name": "Oversized",
                        "stream_order": 1,
                        "blob": "x" * (RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES + 1),
                    },
                    "geometry": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]},
                }
            ],
            "total": 1,
            "feature_total": 1,
            "limit": limit,
            "offset": offset,
        }

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        assert basin_version_id == "basin_v01"
        assert river_network_version_id == "basin_rivnet_v01"
        assert segment_id == "seg_oversized"
        return {
            "river_segment_id": "seg_oversized",
            "river_network_version_id": "basin_rivnet_v01",
            "segment_order": 1,
            "downstream_segment_id": None,
            "length_m": 1234.5,
            "geom": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]},
            "properties_json": {
                "name": "Oversized",
                "blob": "x" * (RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES + 1),
            },
            "created_at": "2026-05-07T00:00:00Z",
        }


class BasinsRiverSegmentStore(FakeModelRegistryStore):
    def list_river_segments(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str | None,
        search: str | None = None,
        stream_order_min: int | None = None,
        stream_order_max: int | None = None,
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

    def get_river_segment(
        self,
        *,
        basin_version_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any]:
        assert basin_version_id == "basins_basin_a_vbasins"
        if river_network_version_id != "basins_basin_a_rivnet_vbasins" or segment_id != "basins_basin_a_shud_seg_1":
            raise MissingResourceError(
                "river_segment_id not found with renderable geometry for "
                f"basin_version_id {basin_version_id}, "
                f"river_network_version_id {river_network_version_id}: {segment_id}"
            )
        return {
            "river_segment_id": "basins_basin_a_shud_seg_1",
            "river_network_version_id": "basins_basin_a_rivnet_vbasins",
            "segment_order": 1,
            "downstream_segment_id": "basins_basin_a_shud_seg_2",
            "length_m": 1234.5,
            "geom": {"type": "LineString", "coordinates": [[90.0, 25.0], [90.5, 25.5]]},
            "properties_json": {
                "segment_id": "basins_basin_a_shud_seg_1",
                "name": "Basins Segment 1",
                "stream_order": 1,
                "basin_slug": "basin-a",
                "shud_input_name": "alias-a",
            },
            "created_at": "2026-05-07T00:00:00Z",
        }


@pytest.fixture
def fake_store() -> FakeModelRegistryStore:
    store = FakeModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    return store


@pytest.fixture(autouse=True)
def clear_overrides() -> None:
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    yield
    if previous_allow_dev_role_header is None:
        os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
    else:
        os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
    app.dependency_overrides.clear()


def _model_admin_headers() -> dict[str, str]:
    return {"X-User-Role": "model_admin"}


@pytest.mark.parametrize(
    ("source_value", "expected_unsafe"),
    [
        ("s3://nhms/sources/basin-a", False),
        ("https://example.test/sources/basin-a", False),
        ("gs://bucket/sources/basin-a", False),
        ("/tmp/nhms/model-root", True),
        ("/volume/data/nwm/Basins/basin-a", True),
        ("file:///tmp/nhms/model-root", True),
        ("C:\\nhms\\model-root", True),
        ("\\\\server\\share\\nhms\\model-root", True),
    ],
)
def test_unsafe_source_value_allows_supported_object_uris_but_blocks_local_paths(
    source_value: str,
    expected_unsafe: bool,
) -> None:
    assert _is_unsafe_source_value(source_value) is expected_unsafe


@pytest.mark.asyncio
async def test_create_basin_rejects_duplicate(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/basins",
            headers=_model_admin_headers(),
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
            headers=_model_admin_headers(),
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
@pytest.mark.parametrize(
    ("path", "payload", "headers", "expected_status", "expected_code"),
    [
        (
            "/api/v1/river-networks",
            {
                "basin_version_id": "basin_v01",
                "version_label": "v01",
                "segments": [
                    {
                        "river_segment_id": f"seg_{index}",
                        "geom": {"type": "LineString", "coordinates": [[90, 25], [91, 26]]},
                    }
                    for index in range(1500)
                ],
            },
            {},
            401,
            "AUTH_REQUIRED",
        ),
        (
            "/api/v1/river-segment-crosswalks",
            {
                "river_network_version_id": "basin_rivnet_v01",
                "entries": [
                    {"river_segment_id": f"seg_{index}", "source": "nwm", "external_id": str(index)}
                    for index in range(1500)
                ],
            },
            {"X-User-Role": "viewer"},
            403,
            "RBAC_FORBIDDEN",
        ),
        (
            "/api/v1/models",
            {
                "model_id": "large_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/large_model/package/",
                "resource_profile": {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[90 + index / 1000, 25 + index / 1000] for index in range(3000)],
                    }
                },
            },
            {},
            401,
            "AUTH_REQUIRED",
        ),
    ],
)
async def test_protected_model_mutation_body_routes_auth_before_large_payload_work(
    fake_store: FakeModelRegistryStore,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    expected_status: int,
    expected_code: str,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(path, json=payload, headers=headers)

    assert response.status_code == expected_status
    body = response.json()
    assert body["error"]["code"] == expected_code
    decision = body["error"]["details"]["policy_decision"]
    assert decision["no_mutation_expected"] is True
    assert fake_store.write_calls == []


@pytest.mark.asyncio
async def test_pre_body_static_denial_preserves_request_id_correlation(
    fake_store: FakeModelRegistryStore,
) -> None:
    request_id = "req-static-pre-body-401"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models",
            headers={"X-Request-ID": request_id},
            json={
                "model_id": "large_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/large_model/package/",
            },
        )

    assert response.status_code == 401
    _assert_pre_body_policy_error(
        response,
        request_id=request_id,
        code="AUTH_REQUIRED",
        action_id="models.switch_version",
        decision="deny",
        target={"type": "model_registry", "id": "models"},
    )
    assert fake_store.write_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "expected_action", "expected_active"),
    [
        (True, "models.activate", True),
        (False, "models.deactivate", True),
    ],
)
async def test_pre_body_dynamic_denial_preserves_request_id_and_canonical_active_action(
    fake_store: FakeModelRegistryStore,
    active: bool,
    expected_action: str,
    expected_active: bool,
) -> None:
    request_id = f"req-dynamic-pre-body-403-{str(active).lower()}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/v1/models/active_model/active",
            headers={"X-Request-ID": request_id, "X-User-Role": "viewer"},
            json={"active": active},
        )

    assert response.status_code == 403
    _assert_pre_body_policy_error(
        response,
        request_id=request_id,
        code="RBAC_FORBIDDEN",
        action_id=expected_action,
        decision="deny",
        target={"type": "model_instance", "id": "active_model"},
    )
    assert fake_store.write_calls == []
    assert fake_store.models["active_model"]["active_flag"] is expected_active


@pytest.mark.asyncio
async def test_pre_body_active_toggle_without_content_length_uses_hard_read_cap(
    fake_store: FakeModelRegistryStore,
) -> None:
    request_id = "req-active-toggle-no-content-length-cap"
    body = b'{"active": true, "padding":"' + (b"x" * 200_000) + b'"}'
    chunks = [body[:8192], body[8192:]]
    reads = 0
    response_messages: list[Message] = []

    async def receive() -> Message:
        nonlocal reads
        if reads < len(chunks):
            chunk = chunks[reads]
            reads += 1
            return {"type": "http.request", "body": chunk, "more_body": reads < len(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        response_messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/api/v1/models/active_model/active",
        "raw_path": b"/api/v1/models/active_model/active",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"x-request-id", request_id.encode()),
            (b"x-user-role", b"model_admin"),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    await app(scope, receive, send)

    start = next(message for message in response_messages if message["type"] == "http.response.start")
    body_bytes = b"".join(
        message.get("body", b"") for message in response_messages if message["type"] == "http.response.body"
    )
    payload = json.loads(body_bytes)
    assert start["status"] == 422
    assert payload["status"] == "error"
    assert payload["request_id"] == request_id
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert reads == 1
    assert fake_store.write_calls == []
    assert fake_store.models["active_model"]["active_flag"] is True


@pytest.mark.asyncio
async def test_pre_body_live_release_block_preserves_request_id_correlation(
    fake_store: FakeModelRegistryStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    monkeypatch.setenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "test_internal")
    monkeypatch.setenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "proof-token")
    request_id = "req-pre-body-503"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/mesh-versions",
            headers={"X-Request-ID": request_id, "X-User-Role": "model_admin"},
            json={
                "basin_version_id": "basin_v01",
                "version_label": "v02",
                "mesh_uri": "s3://nhms/mesh.sp",
            },
        )

    assert response.status_code == 503
    _assert_pre_body_policy_error(
        response,
        request_id=request_id,
        code="RELEASE_BLOCKED",
        action_id="models.switch_version",
        decision="release_blocked",
        target={"type": "model_registry", "id": "mesh-versions"},
    )
    details = response.json()["error"]["details"]
    assert details["policy_decision"]["execution_mode"] == "release_blocked"
    assert details["removal_criteria"] == "Configure and prove live backend identity-provider role mapping."
    assert fake_store.write_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "expected_action", "expected_model_id", "expected_active"),
    [
        (True, "models.activate", "inactive_model", False),
        (False, "models.deactivate", "active_model", True),
    ],
)
async def test_pre_body_active_toggle_live_release_block_uses_canonical_action(
    fake_store: FakeModelRegistryStore,
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
    expected_action: str,
    expected_model_id: str,
    expected_active: bool,
) -> None:
    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    monkeypatch.setenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "test_internal")
    monkeypatch.setenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "proof-token")
    request_id = f"req-active-pre-body-503-{str(active).lower()}"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            f"/api/v1/models/{expected_model_id}/active",
            headers={"X-Request-ID": request_id, "X-User-Role": "model_admin"},
            json={"active": active},
        )

    assert response.status_code == 503
    _assert_pre_body_policy_error(
        response,
        request_id=request_id,
        code="RELEASE_BLOCKED",
        action_id=expected_action,
        decision="release_blocked",
        target={"type": "model_instance", "id": expected_model_id},
    )
    details = response.json()["error"]["details"]
    assert details["policy_decision"]["execution_mode"] == "release_blocked"
    assert details["removal_criteria"] == "Configure and prove live backend identity-provider role mapping."
    assert fake_store.write_calls == []
    assert fake_store.models[expected_model_id]["active_flag"] is expected_active


@pytest.mark.asyncio
async def test_model_registry_admin_write_success_exposes_allowed_policy_evidence(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/mesh-versions",
            headers={"X-User-Role": "model_admin", "X-User-ID": "alice"},
            json={
                "basin_version_id": "basin_v01",
                "version_label": "v02",
                "mesh_uri": "s3://nhms/mesh.sp",
            },
        )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "ok"
    decision = body["auth_policy_decisions"][0]
    assert decision["decision"] == "allow"
    assert decision["action_id"] == "models.switch_version"
    assert decision["actor_id"] == "alice"
    assert decision["roles"] == ["model_admin"]
    assert decision["target"] == {"type": "model_registry", "id": "mesh-versions"}
    assert decision["reason_code"] == "RBAC_ALLOWED"
    assert decision["execution_mode"] == "backend_route_executed"
    assert fake_store.write_calls == ["create_mesh_version"]


@pytest.mark.asyncio
async def test_model_lifecycle_route_runs_preflight_and_activation_with_canonical_policy(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/inactive_model/lifecycle",
            headers={"X-User-Role": "model_admin", "X-User-ID": "alice"},
            json={"operation": "activate"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["data"]["status"] == "allowed"
    assert body["data"]["operation"] == "activate"
    assert body["data"]["model"]["active_flag"] is True
    assert body["data"]["model"]["lifecycle_state"] == "active"
    assert body["data"]["preflight"]["status"] == "ready"
    assert body["data"]["audit_reference"] == {
        "entity_type": "model_instance",
        "entity_id": "inactive_model",
        "log_id": 1,
    }
    decision = body["auth_policy_decisions"][0]
    assert decision["action_id"] == "models.activate"
    assert decision["decision"] == "allow"
    assert fake_store.lifecycle_calls == [("inactive_model", "activate")]
    assert fake_store.models["active_model"]["lifecycle_state"] == "superseded"


@pytest.mark.asyncio
async def test_model_lifecycle_preflight_requires_auth_before_store_call(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/inactive_model/preflight",
            json={"operation": "activate"},
        )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert fake_store.preflight_calls == []
    assert fake_store.lifecycle_calls == []
    assert fake_store.models["inactive_model"]["active_flag"] is False


@pytest.mark.asyncio
async def test_model_lifecycle_preflight_forbidden_role_happens_before_store_call(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/inactive_model/preflight",
            headers={"X-User-Role": "operator", "X-User-ID": "ops"},
            json={"operation": "activate"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "RBAC_FORBIDDEN"
    assert fake_store.preflight_calls == []
    assert fake_store.lifecycle_calls == []
    assert fake_store.models["inactive_model"]["active_flag"] is False


@pytest.mark.asyncio
async def test_model_lifecycle_preflight_authorized_model_admin_passes_policy_roles(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/inactive_model/preflight",
            headers={"X-User-Role": "model_admin", "X-User-ID": "alice"},
            json={"operation": "activate"},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert fake_store.lifecycle_calls == []
    assert len(fake_store.preflight_calls) == 1
    decision = fake_store.preflight_calls[0]["policy_decision"]
    assert decision.action_id == "models.activate"
    assert decision.roles == ("model_admin",)
    assert decision.target_id == "inactive_model"


@pytest.mark.asyncio
async def test_model_lifecycle_preflight_model_admin_override_missing_active_is_blocked(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/active_model/preflight",
            headers={"X-User-Role": "model_admin"},
            json={
                "operation": "deactivate",
                "override_missing_active": True,
                "reason": "planned maintenance",
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "blocked"
    assert data["blockers"] == [
        {"code": "OVERRIDE_REQUIRES_SYS_ADMIN", "message": "Missing-active override requires sys_admin."}
    ]
    assert fake_store.lifecycle_calls == []
    assert fake_store.models["active_model"]["active_flag"] is True


@pytest.mark.asyncio
async def test_model_lifecycle_preflight_sys_admin_override_missing_active_can_be_ready(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/active_model/preflight",
            headers={"X-User-Role": "sys_admin"},
            json={
                "operation": "deactivate",
                "override_missing_active": True,
                "reason": "planned maintenance",
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert data["blockers"] == []
    assert fake_store.lifecycle_calls == []
    assert fake_store.models["active_model"]["active_flag"] is True


@pytest.mark.asyncio
async def test_model_lifecycle_preflight_block_persists_blocked_evidence_without_mutation(
    fake_store: FakeModelRegistryStore,
) -> None:
    fake_store.models["inactive_model"]["model_package_uri"] = "ftp://unsafe/package"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/inactive_model/lifecycle",
            headers={"X-User-Role": "model_admin"},
            json={"operation": "activate", "reason": "unsafe /tmp/local secret-token"},
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["status"] == "blocked"
    assert data["model"]["active_flag"] is False
    assert data["preflight"]["blockers"] == [
        {"code": "OBJECT_URI_PREFIX_INVALID", "message": "Model package URI prefix is not supported."}
    ]
    assert data["preflight"]["reason"] == "[redacted]"
    assert fake_store.models["inactive_model"]["active_flag"] is False
    assert fake_store.models["active_model"]["active_flag"] is True


@pytest.mark.asyncio
async def test_model_lifecycle_rbac_denial_happens_before_store_mutation(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models/inactive_model/lifecycle",
            headers={"X-User-Role": "operator"},
            json={"operation": "activate"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "RBAC_FORBIDDEN"
    assert fake_store.lifecycle_calls == []
    assert fake_store.models["inactive_model"]["active_flag"] is False


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
async def test_get_river_segment_null_geometry_uses_not_found_envelope() -> None:
    store = NullGeometryModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments/seg_001",
            params={"river_network_version_id": "basin_rivnet_v01"},
        )

    assert response.status_code == 404
    _assert_error_envelope(
        response.json(),
        code="MODEL_REGISTRY_NOT_FOUND",
        message_contains="renderable geometry",
        error_type="MissingResourceError",
    )


@pytest.mark.asyncio
async def test_get_river_segment_oversized_geometry_uses_not_found_envelope() -> None:
    store = OversizedGeometryModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments/seg_001",
            params={"river_network_version_id": "basin_rivnet_v01"},
        )

    assert response.status_code == 404
    _assert_error_envelope(
        response.json(),
        code="MODEL_REGISTRY_NOT_FOUND",
        message_contains="renderable geometry",
        error_type="MissingResourceError",
    )


@pytest.mark.asyncio
async def test_get_river_segment_returns_declared_detail_shape(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments/seg_001",
            params={"river_network_version_id": "basin_rivnet_v01"},
        )

    assert fake_store is not None
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    payload = body["data"]
    assert set(payload) == {
        "river_segment_id",
        "river_network_version_id",
        "segment_order",
        "downstream_segment_id",
        "length_m",
        "geom",
        "properties_json",
        "created_at",
    }
    assert payload["river_segment_id"] == "seg_001"
    assert payload["river_network_version_id"] == "basin_rivnet_v01"
    assert payload["segment_order"] == 2
    assert payload["downstream_segment_id"] is None
    assert payload["length_m"] == 1234.5
    assert payload["geom"]["type"] == "LineString"
    assert payload["properties_json"]["segment_id"] == "seg_001"
    assert payload["created_at"] == "2026-05-07T00:00:00Z"


@pytest.mark.asyncio
async def test_get_river_segment_requires_river_network_version_id(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/basin-versions/basin_v01/river-segments/seg_001")

    assert fake_store is not None
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_river_segment_duplicate_ids_return_selected_network_row() -> None:
    store = DuplicateSegmentIdModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments/seg_shared",
            params={"river_network_version_id": "rivnet_selected"},
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["river_segment_id"] == "seg_shared"
    assert payload["river_network_version_id"] == "rivnet_selected"
    assert payload["segment_order"] == 2
    assert payload["geom"]["coordinates"] == [[91.0, 26.0], [91.5, 26.5]]
    assert payload["properties_json"]["name"] == "Selected sibling network row"


@pytest.mark.asyncio
async def test_river_segment_collection_oversized_properties_return_413_envelope() -> None:
    store = OversizedPropertiesModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments",
            params={"river_network_version_id": "basin_rivnet_v01", "limit": 100, "offset": 0},
        )

    assert response.status_code == 413
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "RIVER_SEGMENT_GEOJSON_BUDGET_EXCEEDED"
    assert body["error"]["details"]["limit_type"] == "serialized_bytes"
    assert body["error"]["details"]["scope"] == "collection"


@pytest.mark.asyncio
async def test_river_segment_detail_oversized_properties_return_413_envelope() -> None:
    store = OversizedPropertiesModelRegistryStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments/seg_oversized",
            params={"river_network_version_id": "basin_rivnet_v01"},
        )

    assert response.status_code == 413
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "RIVER_SEGMENT_GEOJSON_BUDGET_EXCEEDED"
    assert body["error"]["details"]["limit_type"] == "serialized_bytes"
    assert body["error"]["details"]["scope"] == "detail"


@pytest.mark.asyncio
async def test_get_missing_river_segment_uses_not_found_envelope(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basin_v01/river-segments/missing_seg",
            params={"river_network_version_id": "basin_rivnet_v01"},
        )

    assert fake_store is not None
    assert response.status_code == 404
    _assert_error_envelope(
        response.json(),
        code="MODEL_REGISTRY_NOT_FOUND",
        message_contains="missing_seg",
        error_type="MissingResourceError",
    )


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
async def test_basins_river_segment_detail_api_returns_backend_geometry() -> None:
    store = BasinsRiverSegmentStore()
    app.dependency_overrides[get_model_registry_store] = lambda: store
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/basin-versions/basins_basin_a_vbasins/river-segments/basins_basin_a_shud_seg_1",
            params={"river_network_version_id": "basins_basin_a_rivnet_vbasins"},
        )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["river_segment_id"] == "basins_basin_a_shud_seg_1"
    assert payload["river_network_version_id"] == "basins_basin_a_rivnet_vbasins"
    assert payload["segment_order"] == 1
    assert payload["downstream_segment_id"] == "basins_basin_a_shud_seg_2"
    assert payload["length_m"] == 1234.5
    assert payload["geom"] == {"type": "LineString", "coordinates": [[90.0, 25.0], [90.5, 25.5]]}
    assert payload["properties_json"]["basin_slug"] == "basin-a"
    assert payload["properties_json"]["shud_input_name"] == "alias-a"


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
    request_id = "req-activate"
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        active_response = await client.put(
            "/api/v1/models/inactive_model/active",
            json={"active": True},
            headers={**_model_admin_headers(), "X-Request-ID": request_id},
        )
        conflict_response = await client.put(
            "/api/v1/models/inactive_model/active",
            json={"active": True},
            headers=_model_admin_headers(),
        )
        missing_response = await client.put(
            "/api/v1/models/missing_model/active",
            json={"active": True},
            headers=_model_admin_headers(),
        )

    assert fake_store.models["inactive_model"]["active_flag"] is True
    assert active_response.status_code == 200
    assert active_response.headers["X-Request-ID"] == request_id
    active_body = active_response.json()
    assert active_body["status"] == "ok"
    assert active_body["request_id"] == request_id
    assert active_body["data"]["status"] == "allowed"
    assert active_body["data"]["model"]["active_flag"] is True
    assert active_body["auth_policy_decisions"][0]["request_id"] == request_id
    assert fake_store.lifecycle_calls == [
        ("inactive_model", "activate"),
        ("inactive_model", "activate"),
        ("missing_model", "activate"),
    ]
    assert fake_store.activation_audit_rows == []
    assert conflict_response.status_code == 200
    assert conflict_response.json()["data"]["model"]["active_flag"] is True
    assert missing_response.status_code == 404
    _assert_error_envelope(
        missing_response.json(),
        code="MODEL_REGISTRY_NOT_FOUND",
        message_contains="missing_model",
        error_type="MissingResourceError",
    )
    assert fake_store.activation_audit_rows == []


@pytest.mark.asyncio
async def test_active_toggle_allows_model_admin_activation_and_sys_admin_deactivation(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        activation = await client.put(
            "/api/v1/models/inactive_model/active",
            json={"active": True},
            headers={"X-User-Role": "model_admin", "X-User-ID": "model-admin"},
        )
        deactivation = await client.put(
            "/api/v1/models/active_model/active",
            json={"active": False},
            headers={"X-User-Role": "sys_admin", "X-User-ID": "sys-admin"},
        )

    assert activation.status_code == 200
    assert deactivation.status_code == 200
    assert fake_store.models["inactive_model"]["active_flag"] is True
    assert fake_store.models["active_model"]["active_flag"] is False
    assert deactivation.json()["data"]["model"]["active_flag"] is False
    decisions = [
        response.json()["auth_policy_decisions"][0]
        for response in (activation, deactivation)
    ]
    assert [
        (decision["action_id"], decision["target"], decision["decision"])
        for decision in decisions
    ] == [
        ("models.activate", {"type": "model_instance", "id": "inactive_model"}, "allow"),
        ("models.deactivate", {"type": "model_instance", "id": "active_model"}, "allow"),
    ]


@pytest.mark.asyncio
async def test_legacy_active_toggle_returns_blocked_lifecycle_evidence(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.put(
            "/api/v1/models/active_model/active",
            json={"active": False},
            headers={"X-User-Role": "model_admin", "X-User-ID": "model-admin"},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "blocked"
    assert data["operation"] == "deactivate"
    assert data["model"]["active_flag"] is True
    assert data["audit_reference"] == {"entity_type": "model_instance", "entity_id": "active_model", "log_id": 1}
    assert {item["code"] for item in data["preflight"]["blockers"]} == {"MISSING_ACTIVE_RISK"}
    assert fake_store.models["active_model"]["active_flag"] is True


@pytest.mark.asyncio
async def test_basins_inactive_model_listing_then_explicit_activation(fake_store: FakeModelRegistryStore) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        default_before = await client.get("/api/v1/models")
        inactive_before = await client.get("/api/v1/models", params={"active": "false"})
        all_before = await client.get("/api/v1/models", params={"active": "all"})
        activation = await client.put(
            "/api/v1/models/inactive_model/active",
            json={"active": True},
            headers=_model_admin_headers(),
        )
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

    activated = activation.json()["data"]["model"]
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
        "model_package_uri": "s3://nhms/models/inactive_model/package/",
        "active_flag": False,
        "manifest_uri": "s3://nhms/models/inactive_model/v1/manifest.json",
        "basin_slug": "basin-a",
        "shud_input_name": "basin_a",
    }.items() <= data.items()
    assert data["mesh_checksum"] is None
    assert data["package_checksum"] is None
    assert data["source_inventory_checksum"] is None


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
    headers = _model_admin_headers()
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await getattr(client, request_method)(path, json=payload, headers=headers)

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
            headers=_model_admin_headers(),
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


@pytest.mark.asyncio
async def test_create_model_rejects_active_flag_payload_before_store_call(
    fake_store: FakeModelRegistryStore,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/models",
            headers=_model_admin_headers(),
            json={
                "model_id": "active_create",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/active_create/package/",
                "active_flag": True,
            },
        )

    assert response.status_code == 422
    assert fake_store.write_calls == []
    assert "active_flag=true is not accepted" in response.text


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
            },
            trusted_internal=True,
        )

    assert not any("INSERT INTO core.model_instance" in statement for statement in cursor.statements)


def test_create_model_rejects_active_flag_before_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered_transaction = [0]

    class FakeTransaction:
        def __enter__(self) -> object:
            entered_transaction[0] += 1
            return object()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(InvalidPayloadError, match="active_flag=true is not accepted"):
        store.create_model(
            {
                "model_id": "demo_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "rivnet_v01",
                "mesh_version_id": "mesh_v01",
                "calibration_version_id": "cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/demo/package/",
                "active_flag": True,
            },
            trusted_internal=True,
        )

    assert entered_transaction == [0]


class _RegistryAdminWriteFakeCursor:
    def __init__(self, rows: list[dict[str, Any] | None]) -> None:
        self.rows = rows
        self.statements: list[str] = []
        self.parameters: list[Any] = []

    def execute(self, statement: str, parameters: Any = ()) -> None:
        self.statements.append(statement)
        self.parameters.append(parameters)

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows.pop(0)


class _RegistryAdminWriteFakeTransaction:
    def __init__(self, cursor: _RegistryAdminWriteFakeCursor, enter_count: list[int]) -> None:
        self.cursor = cursor
        self.enter_count = enter_count

    def __enter__(self) -> _RegistryAdminWriteFakeCursor:
        self.enter_count[0] += 1
        return self.cursor

    def __exit__(self, *_args: object) -> bool:
        return False


def _registry_admin_policy_decision(target_id: str) -> Any:
    return evaluate_policy(
        AuthContext(
            actor_id="dev-test:model-admin",
            roles=("model_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "models.switch_version",
        target_type="model_registry",
        target_id=target_id,
    )


_MULTIPOLYGON = {
    "type": "MultiPolygon",
    "coordinates": [[[[90.0, 25.0], [91.0, 25.0], [91.0, 26.0], [90.0, 26.0], [90.0, 25.0]]]],
}
_LINESTRING = {"type": "LineString", "coordinates": [[90.0, 25.0], [91.0, 26.0]]}
_REGISTRY_ADMIN_WRITE_CASES = (
    pytest.param(
        "create_basin_with_version",
        "basins",
        lambda store: store.create_basin_with_version(
            {
                "basin_id": "basin_a",
                "basin_name": "Basin A",
                "basin_version": {"version_label": "v01", "geom": _MULTIPOLYGON},
            }
        ),
        lambda store, kwargs: store.create_basin_with_version(
            {
                "basin_id": "basin_a",
                "basin_name": "Basin A",
                "basin_version": {"version_label": "v01", "geom": _MULTIPOLYGON},
            },
            **kwargs,
        ),
        [
            None,
            {"basin_id": "basin_a", "basin_name": "Basin A"},
            {"basin_version_id": "basin_a_v01", "basin_id": "basin_a", "version_label": "v01"},
        ],
        id="create_basin_with_version",
    ),
    pytest.param(
        "create_basin_version",
        "basin_a",
        lambda store: store.create_basin_version("basin_a", {"version_label": "v02", "geom": _MULTIPOLYGON}),
        lambda store, kwargs: store.create_basin_version(
            "basin_a",
            {"version_label": "v02", "geom": _MULTIPOLYGON},
            **kwargs,
        ),
        [
            {"exists": 1},
            {"basin_version_id": "basin_a_v02", "basin_id": "basin_a", "version_label": "v02"},
        ],
        id="create_basin_version",
    ),
    pytest.param(
        "create_river_network",
        "river-networks",
        lambda store: store.create_river_network(
            {
                "basin_version_id": "basin_a_v01",
                "version_label": "v01",
                "segments": [{"river_segment_id": "seg_1", "geom": _LINESTRING}],
            }
        ),
        lambda store, kwargs: store.create_river_network(
            {
                "basin_version_id": "basin_a_v01",
                "version_label": "v01",
                "segments": [{"river_segment_id": "seg_1", "geom": _LINESTRING}],
            },
            **kwargs,
        ),
        [
            {"exists": 1},
            {"river_network_version_id": "basin_a_v01_rivnet_v01", "basin_version_id": "basin_a_v01"},
        ],
        id="create_river_network",
    ),
    pytest.param(
        "create_mesh_version",
        "mesh-versions",
        lambda store: store.create_mesh_version(
            {"basin_version_id": "basin_a_v01", "version_label": "v01", "mesh_uri": "s3://nhms/mesh.sp"}
        ),
        lambda store, kwargs: store.create_mesh_version(
            {"basin_version_id": "basin_a_v01", "version_label": "v01", "mesh_uri": "s3://nhms/mesh.sp"},
            **kwargs,
        ),
        [
            {"exists": 1},
            {"mesh_version_id": "basin_a_v01_mesh_v01", "basin_version_id": "basin_a_v01"},
        ],
        id="create_mesh_version",
    ),
    pytest.param(
        "create_model",
        "models",
        lambda store: store.create_model(
            {
                "model_id": "model_a",
                "basin_version_id": "basin_a_v01",
                "river_network_version_id": "rivnet_v01",
                "mesh_version_id": "mesh_v01",
                "calibration_version_id": "cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/model_a/package/",
            }
        ),
        lambda store, kwargs: store.create_model(
            {
                "model_id": "model_a",
                "basin_version_id": "basin_a_v01",
                "river_network_version_id": "rivnet_v01",
                "mesh_version_id": "mesh_v01",
                "calibration_version_id": "cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/model_a/package/",
            },
            **kwargs,
        ),
        [
            {"exists": 1},
            {"basin_version_id": "basin_a_v01"},
            {"basin_version_id": "basin_a_v01"},
            {
                "model_id": "model_a",
                "basin_version_id": "basin_a_v01",
                "river_network_version_id": "rivnet_v01",
                "mesh_version_id": "mesh_v01",
                "calibration_version_id": "cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/model_a/package/",
            },
        ],
        id="create_model",
    ),
    pytest.param(
        "create_crosswalk_entries",
        "river-segment-crosswalks",
        lambda store: store.create_crosswalk_entries(
            {
                "river_network_version_id": "rivnet_v01",
                "entries": [{"river_segment_id": "seg_1", "source": "nwm", "external_id": "1001"}],
            }
        ),
        lambda store, kwargs: store.create_crosswalk_entries(
            {
                "river_network_version_id": "rivnet_v01",
                "entries": [{"river_segment_id": "seg_1", "source": "nwm", "external_id": "1001"}],
            },
            **kwargs,
        ),
        [],
        id="create_crosswalk_entries",
    ),
)


@pytest.mark.parametrize(
    ("method_name", "target_id", "unauthorized_call", "_authorized_call", "_rows"),
    _REGISTRY_ADMIN_WRITE_CASES,
)
def test_m17_registry_admin_write_methods_require_policy_before_transaction(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    target_id: str,
    unauthorized_call: Any,
    _authorized_call: Any,
    _rows: list[dict[str, Any] | None],
) -> None:
    del method_name, target_id, _authorized_call, _rows
    entered_transaction = [0]
    cursor = _RegistryAdminWriteFakeCursor([])
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_transaction",
        lambda _self: _RegistryAdminWriteFakeTransaction(cursor, entered_transaction),
    )
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(ModelRegistryError, match="Authentication is required"):
        unauthorized_call(store)

    assert entered_transaction == [0]
    assert cursor.statements == []


@pytest.mark.parametrize(
    ("method_name", "target_id", "_unauthorized_call", "authorized_call", "rows"),
    _REGISTRY_ADMIN_WRITE_CASES,
)
def test_m17_registry_admin_write_methods_accept_current_route_policy_evidence(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    target_id: str,
    _unauthorized_call: Any,
    authorized_call: Any,
    rows: list[dict[str, Any] | None],
) -> None:
    del method_name, _unauthorized_call
    entered_transaction = [0]
    cursor = _RegistryAdminWriteFakeCursor(list(rows))
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_transaction",
        lambda _self: _RegistryAdminWriteFakeTransaction(cursor, entered_transaction),
    )
    monkeypatch.setattr(PsycopgModelRegistryStore, "_json", lambda _self, value: dict(value))

    def fake_execute_values(
        _self: PsycopgModelRegistryStore,
        cursor: _RegistryAdminWriteFakeCursor,
        statement: str,
        rows: list[tuple[Any, ...]],
        *,
        template: str | None = None,
        fetch: bool = False,
    ) -> list[dict[str, Any]]:
        del _self, template
        cursor.execute(statement, rows)
        if not fetch:
            return []
        return [
            {
                "river_network_version_id": row[0],
                "river_segment_id": row[1],
                "source": row[2],
                "external_id": row[3],
                "properties_json": row[4],
            }
            for row in rows
        ]

    monkeypatch.setattr(PsycopgModelRegistryStore, "_execute_values", fake_execute_values)
    store = PsycopgModelRegistryStore("postgresql://example")

    result = authorized_call(store, {"policy_decision": _registry_admin_policy_decision(target_id)})

    assert result
    assert entered_transaction == [1]
    assert cursor.statements


@pytest.mark.parametrize(
    ("method_name", "_target_id", "_unauthorized_call", "authorized_call", "rows"),
    _REGISTRY_ADMIN_WRITE_CASES,
)
def test_m17_registry_admin_write_methods_accept_explicit_trusted_internal(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    _target_id: str,
    _unauthorized_call: Any,
    authorized_call: Any,
    rows: list[dict[str, Any] | None],
) -> None:
    del method_name, _target_id, _unauthorized_call
    entered_transaction = [0]
    cursor = _RegistryAdminWriteFakeCursor(list(rows))
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_transaction",
        lambda _self: _RegistryAdminWriteFakeTransaction(cursor, entered_transaction),
    )
    monkeypatch.setattr(PsycopgModelRegistryStore, "_json", lambda _self, value: dict(value))
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_execute_values",
        lambda _self, cursor, statement, rows, *, template=None, fetch=False: [
            {
                "river_network_version_id": row[0],
                "river_segment_id": row[1],
                "source": row[2],
                "external_id": row[3],
                "properties_json": row[4],
            }
            for row in rows
        ]
        if fetch
        else [],
    )
    store = PsycopgModelRegistryStore("postgresql://example")

    result = authorized_call(store, {"trusted_internal": True})

    assert result
    assert entered_transaction == [1]


def test_set_model_active_delegates_to_lifecycle_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_lifecycle(self: PsycopgModelRegistryStore, model_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"model_id": model_id, **kwargs})
        return {
            "status": "allowed",
            "operation": kwargs["operation"],
            "model": {
                "model_id": model_id,
                "active_flag": kwargs["operation"] == "activate",
                "lifecycle_state": "active" if kwargs["operation"] == "activate" else "inactive",
                "resource_profile": {},
            },
        }

    monkeypatch.setattr(PsycopgModelRegistryStore, "model_lifecycle_operation", fake_lifecycle)
    store = PsycopgModelRegistryStore("postgresql://example")

    result = store.set_model_active("basins_model", True, trusted_internal=True, request_id="req-activate")

    assert result["active_flag"] is True
    assert calls == [
        {
            "model_id": "basins_model",
            "operation": "activate",
            "policy_decision": None,
            "trusted_internal": True,
            "request_id": "req-activate",
        }
    ]


def test_set_model_active_writes_explicit_request_id_to_activation_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_lifecycle(self: PsycopgModelRegistryStore, model_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"model_id": model_id, **kwargs})
        return {"status": "allowed", "operation": kwargs["operation"], "model": {"model_id": model_id}}

    monkeypatch.setattr(PsycopgModelRegistryStore, "model_lifecycle_operation", fake_lifecycle)
    store = PsycopgModelRegistryStore("postgresql://example")

    store.set_model_active("basins_model", True, trusted_internal=True, request_id="req-activate")

    assert calls[0]["request_id"] == "req-activate"


def test_set_model_active_direct_policy_evidence_generates_activation_audit_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_lifecycle(self: PsycopgModelRegistryStore, model_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"model_id": model_id, **kwargs})
        return {"status": "allowed", "operation": kwargs["operation"], "model": {"model_id": model_id}}

    monkeypatch.setattr(PsycopgModelRegistryStore, "model_lifecycle_operation", fake_lifecycle)
    store = PsycopgModelRegistryStore("postgresql://example")
    decision = evaluate_policy(
        AuthContext(
            actor_id="external-admin",
            roles=("model_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "models.activate",
        target_type="model_instance",
        target_id="basins_model",
    )

    store.set_model_active("basins_model", True, policy_decision=decision)

    assert calls[0]["policy_decision"] is decision
    assert calls[0]["operation"] == "activate"


def test_set_model_active_direct_call_requires_policy_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            self.statements.append(statement)

        def fetchone(self) -> dict[str, Any] | None:
            return None

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

    with pytest.raises(ModelRegistryError, match="Authentication is required"):
        store.set_model_active("basins_model", True)

    assert cursor.statements == []


def test_preflight_model_operation_uses_policy_roles_for_missing_active_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> bool:
            return False

    model = {
        "model_id": "active_model",
        "basin_id": "basin",
        "basin_name": "Basin",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "basin_rivnet_v01",
        "mesh_version_id": "basin_mesh_v01",
        "model_package_uri": "s3://nhms/models/active_model/package/",
        "package_checksum": "package-sha-1",
        "resource_profile": {"package_checksum": "package-sha-1"},
        "active_flag": True,
        "lifecycle_state": "active",
    }
    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_model_lifecycle_row",
        lambda _self, _cursor, model_id, *, for_update: dict(model) if model_id == "active_model" else None,
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_active_model_for_scope",
        lambda _self, _cursor, _basin_version_id, *, for_update: dict(model),
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_trustworthy_rollback_history",
        lambda *_args, **_kwargs: None,
    )
    store = PsycopgModelRegistryStore("postgresql://example")
    model_admin_decision = evaluate_policy(
        AuthContext(
            actor_id="model-admin",
            roles=("model_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "models.deactivate",
        target_type="model_instance",
        target_id="active_model",
    )
    sys_admin_decision = evaluate_policy(
        AuthContext(
            actor_id="sys-admin",
            roles=("sys_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "models.deactivate",
        target_type="model_instance",
        target_id="active_model",
    )

    model_admin_preflight = store.preflight_model_operation(
        "active_model",
        operation="deactivate",
        policy_decision=model_admin_decision,
        override_missing_active=True,
        reason="planned maintenance",
    )
    sys_admin_preflight = store.preflight_model_operation(
        "active_model",
        operation="deactivate",
        policy_decision=sys_admin_decision,
        override_missing_active=True,
        reason="planned maintenance",
    )

    assert model_admin_preflight["status"] == "blocked"
    assert {item["code"] for item in model_admin_preflight["blockers"]} == {"OVERRIDE_REQUIRES_SYS_ADMIN"}
    assert sys_admin_preflight["status"] == "ready"
    assert sys_admin_preflight["blockers"] == []
    assert sys_admin_preflight["action_id"] == "models.deactivate"
    assert sys_admin_preflight["actor_id"] == "sys-admin"
    assert sys_admin_preflight["roles"] == ["sys_admin"]


def test_rollback_preflight_lineage_uses_restored_model_mesh_properties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> bool:
            return False

    current_model = {
        "model_id": "current_active",
        "basin_id": "basin",
        "basin_name": "Basin",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "current_rivnet_v01",
        "mesh_version_id": "current_mesh_v01",
        "model_package_uri": "s3://nhms/models/current_active/package/",
        "package_checksum": "current-package-sha",
        "resource_profile": {
            "package_checksum": "current-package-sha",
            "manifest_uri": "s3://nhms/models/current_active/manifest.json",
        },
        "mesh_properties_json": {"mesh_property_marker": "outgoing-current"},
        "active_flag": True,
        "lifecycle_state": "active",
    }
    restored_model = {
        **current_model,
        "model_id": "previous_model",
        "river_network_version_id": "restored_rivnet_v01",
        "mesh_version_id": "restored_mesh_v01",
        "model_package_uri": "s3://nhms/models/previous_model/package/",
        "package_checksum": "restored-package-sha",
        "resource_profile": {
            "package_checksum": "restored-package-sha",
            "manifest_uri": "s3://nhms/models/previous_model/manifest.json",
        },
        "mesh_properties_json": {"mesh_property_marker": "restored-previous"},
        "active_flag": False,
        "lifecycle_state": "superseded",
    }
    rows = {
        current_model["model_id"]: current_model,
        restored_model["model_id"]: restored_model,
    }

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_model_lifecycle_row",
        lambda _self, _cursor, model_id, *, for_update: dict(rows[model_id]) if model_id in rows else None,
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_active_model_for_scope",
        lambda _self, _cursor, _basin_version_id, *, for_update: dict(current_model),
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_trustworthy_rollback_history",
        lambda *_args, **_kwargs: {
            "trusted": True,
            "prior_audit_log_id": 7,
            "matched_previous_model_id": restored_model["model_id"],
        },
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_idempotent_rollback_retry_history",
        lambda *_args, **_kwargs: None,
    )
    store = PsycopgModelRegistryStore("postgresql://example")
    decision = evaluate_policy(
        AuthContext(
            actor_id="model-admin",
            roles=("model_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "models.rollback_version",
        target_type="model_instance",
        target_id="current_active",
    )

    preflight = store.preflight_model_operation(
        "current_active",
        operation="rollback_version",
        policy_decision=decision,
        previous_model_id="previous_model",
    )

    assert preflight["restored_model_id"] == "previous_model"
    assert preflight["mesh_version_id"] == "restored_mesh_v01"
    assert preflight["lineage"]["mesh_properties"] == {"mesh_property_marker": "restored-previous"}


def test_model_lifecycle_non_rollback_ignores_idempotent_rollback_retry_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> bool:
            return False

    superseded_model = {
        "model_id": "rolled_back_from",
        "model_name": "rolled_back_from",
        "basin_id": "basin",
        "basin_name": "Basin",
        "basin_version_id": "basin_v01",
        "river_network_version_id": "basin_rivnet_v01",
        "mesh_version_id": "basin_mesh_v01",
        "calibration_version_id": "basin_cal_v01",
        "shud_code_version": "2.0",
        "model_package_uri": "s3://nhms/models/rolled_back_from/package/",
        "package_checksum": "package-sha-1",
        "resource_profile": {"package_checksum": "package-sha-1"},
        "active_flag": False,
        "lifecycle_state": "superseded",
        "segment_count": 1,
    }
    current_active = {
        **superseded_model,
        "model_id": "current_active",
        "model_name": "current_active",
        "model_package_uri": "s3://nhms/models/current_active/package/",
        "active_flag": True,
        "lifecycle_state": "active",
    }
    rows = {
        superseded_model["model_id"]: superseded_model,
        current_active["model_id"]: current_active,
    }
    idempotent_retry_calls: list[dict[str, Any]] = []

    def fetch_model_row(
        _self: PsycopgModelRegistryStore,
        _cursor: object,
        model_id: str,
        *,
        for_update: bool,
    ) -> dict[str, Any] | None:
        row = rows.get(model_id)
        return dict(row) if row is not None else None

    def update_lifecycle_state(
        _self: PsycopgModelRegistryStore,
        _cursor: object,
        model_id: str,
        lifecycle_state: str,
    ) -> dict[str, Any]:
        rows[model_id] = {
            **rows[model_id],
            "lifecycle_state": lifecycle_state,
            "active_flag": lifecycle_state == "active",
        }
        return dict(rows[model_id])

    def fetch_idempotent_retry(
        _self: PsycopgModelRegistryStore,
        _cursor: object,
        **kwargs: Any,
    ) -> dict[str, Any]:
        idempotent_retry_calls.append(kwargs)
        return {
            "trusted": True,
            "prior_audit_log_id": 7,
            "matched_previous_model_id": current_active["model_id"],
        }

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    monkeypatch.setattr(PsycopgModelRegistryStore, "_lock_basin_version_scope", lambda *_args: None)
    monkeypatch.setattr(PsycopgModelRegistryStore, "_fetch_model_lifecycle_row", fetch_model_row)
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_active_model_for_scope",
        lambda _self, _cursor, _basin_version_id, *, for_update: dict(current_active),
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_trustworthy_rollback_history",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        PsycopgModelRegistryStore,
        "_fetch_idempotent_rollback_retry_history",
        fetch_idempotent_retry,
    )
    monkeypatch.setattr(PsycopgModelRegistryStore, "_update_model_lifecycle_state", update_lifecycle_state)
    monkeypatch.setattr(PsycopgModelRegistryStore, "_insert_model_lifecycle_audit", lambda *_args, **_kwargs: 42)
    store = PsycopgModelRegistryStore("postgresql://example")
    decision = evaluate_policy(
        AuthContext(
            actor_id="model-admin",
            roles=("model_admin",),
            auth_mode="dev_test",
            live_backend_auth_executed=False,
        ),
        "models.deactivate",
        target_type="model_instance",
        target_id="rolled_back_from",
    )

    result = store.model_lifecycle_operation(
        "rolled_back_from",
        operation="deprecate",
        policy_decision=decision,
        previous_model_id="current_active",
    )

    assert idempotent_retry_calls == []
    assert result["status"] == "allowed"
    assert result["operation"] == "deprecate"
    assert result["model"]["model_id"] == "rolled_back_from"
    assert result["model"]["lifecycle_state"] == "deprecated"
    assert result["audit_reference"]["log_id"] == 42


def test_set_model_active_missing_does_not_write_legacy_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_lifecycle(self: PsycopgModelRegistryStore, model_id: str, **_kwargs: Any) -> dict[str, Any]:
        raise MissingResourceError(f"model_id not found: {model_id}")

    monkeypatch.setattr(PsycopgModelRegistryStore, "model_lifecycle_operation", fake_lifecycle)
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(MissingResourceError):
        store.set_model_active("missing_model", True, trusted_internal=True)



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
                        "local_path": "/tmp/nhms/private/model-root",
                        "artifact": {
                            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "sha1": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                            "hash": "hash-secret",
                            "digest": "digest-secret",
                        },
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
    assert detail["mesh_checksum"] is None
    assert detail["package_checksum"] is None
    assert detail["manifest_uri"] == "s3://nhms/models/basins_basin_a_shud/vbasins/manifest.json"
    assert detail["source_inventory_checksum"] is None
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
        None,
    ]
    assert detail["resource_profile"]["source_lineage"]["label"] == "s3 path label, not a URI"
    assert detail["resource_profile"]["source_lineage"]["local_path"] is None
    assert detail["resource_profile"]["source_lineage"]["artifact"]["sha256"] is None
    assert detail["resource_profile"]["source_lineage"]["artifact"]["sha1"] is None
    assert detail["resource_profile"]["source_lineage"]["artifact"]["hash"] is None
    assert detail["resource_profile"]["source_lineage"]["artifact"]["digest"] is None
    public_detail_json = json.dumps(detail)
    for token in (
        "/volume/data",
        "/tmp/nhms/private/model-root",
        "C:\\",
        "file://",
        "token=secret",
        "user:pass@",
        "#frag",
        "package-sha-1",
        "inventory-sha-1",
        "mesh-sha-1",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "hash-secret",
        "digest-secret",
    ):
        assert token not in public_detail_json
    assert "mesh_properties_json" not in detail


def test_get_river_segment_binds_selected_river_network_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.parameters: tuple[Any, ...] | None = None

        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> dict[str, Any]:
            assert self.parameters == (
                "basin_v01",
                "seg_001",
                "rivnet_selected",
                10_000,
                3,
            )
            assert "rs.river_network_version_id = %s" in self.statement
            return {
                "river_segment_id": "seg_001",
                "river_network_version_id": "rivnet_selected",
                "segment_order": 7,
                "downstream_segment_id": "seg_002",
                "length_m": 42.5,
                "geom": {"type": "LineString", "coordinates": [[91.0, 26.0], [92.0, 27.0]]},
                "properties_json": {"name": "Selected sibling network segment"},
                "created_at": "2026-05-19T00:00:00Z",
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

    detail = store.get_river_segment(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_selected",
        segment_id="seg_001",
    )

    assert detail["river_segment_id"] == "seg_001"
    assert detail["river_network_version_id"] == "rivnet_selected"
    assert detail["geom"]["coordinates"] == [[91.0, 26.0], [92.0, 27.0]]
    assert detail["segment_order"] == 7


def test_list_river_segments_excludes_oversized_collection_geometry_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls = 0
            self.statement = ""
            self.parameters: tuple[Any, ...] | None = None

        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.calls += 1
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> dict[str, Any]:
            assert self.calls == 1
            assert "ST_NPoints(geom) BETWEEN 2 AND %s" in self.statement
            assert "ST_NDims(geom) <= %s" in self.statement
            assert "running_coordinate_count <= %s" in self.statement
            assert self.parameters == (
                "basin_v01",
                "rivnet_v01",
                10_000,
                3,
                RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
            )
            return {"total": 1, "feature_total": 0}

        def fetchall(self) -> list[dict[str, Any]]:
            assert self.calls == 2
            assert "ST_AsGeoJSON(geom)::json AS geometry" in self.statement
            assert "ST_NPoints(rs.geom) BETWEEN 2 AND %s" in self.statement
            assert "ST_NDims(rs.geom) <= %s" in self.statement
            assert "running_coordinate_count <= %s" in self.statement
            assert self.statement.index("ST_NPoints(rs.geom) BETWEEN 2 AND %s") < self.statement.index(
                "ST_AsGeoJSON(geom)::json AS geometry"
            )
            assert self.statement.index("running_coordinate_count <= %s") < self.statement.index(
                "ST_AsGeoJSON(geom)::json AS geometry"
            )
            assert self.parameters == (
                "basin_v01",
                "rivnet_v01",
                10_000,
                3,
                RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
                100,
                0,
            )
            return []

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

    collection = store.list_river_segments(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_v01",
        limit=100,
        offset=0,
    )

    assert collection["total"] == 1
    assert collection["feature_total"] == 0
    assert collection["features"] == []


def test_list_river_segments_applies_aggregate_collection_budget_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls = 0
            self.statement = ""
            self.parameters: tuple[Any, ...] | None = None

        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.calls += 1
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> dict[str, Any]:
            assert self.calls == 1
            assert "SUM(ST_NPoints(geom)) OVER" in self.statement
            assert "running_coordinate_count <= %s" in self.statement
            assert self.parameters == (
                "basin_v01",
                "rivnet_v01",
                10_000,
                3,
                RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
            )
            return {"total": 6, "feature_total": 5}

        def fetchall(self) -> list[dict[str, Any]]:
            assert self.calls == 2
            assert "SUM(ST_NPoints(rs.geom)) OVER" in self.statement
            assert "running_coordinate_count <= %s" in self.statement
            assert "ST_AsGeoJSON(geom)::json AS geometry" in self.statement
            assert self.statement.index("running_coordinate_count <= %s") < self.statement.index(
                "ST_AsGeoJSON(geom)::json AS geometry"
            )
            assert self.parameters == (
                "basin_v01",
                "rivnet_v01",
                10_000,
                3,
                RIVER_SEGMENT_COLLECTION_PAGE_MAX_COORDINATES,
                100,
                0,
            )
            return [
                {
                    "river_segment_id": f"seg_{index:03d}",
                    "river_network_version_id": "rivnet_v01",
                    "basin_version_id": "basin_v01",
                    "segment_order": index,
                    "downstream_segment_id": None,
                    "length_m": 1000,
                    "properties_json": {},
                    "geometry": {"type": "LineString", "coordinates": [[91.0, 26.0], [92.0, 27.0]]},
                }
                for index in range(1, 6)
            ]

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

    collection = store.list_river_segments(
        basin_version_id="basin_v01",
        river_network_version_id="rivnet_v01",
        limit=100,
        offset=0,
    )

    assert collection["total"] == 6
    assert collection["feature_total"] == 5
    assert len(collection["features"]) == 5
    assert collection["features"][-1]["properties"]["river_segment_id"] == "seg_005"


def test_list_river_segments_rejects_serialized_payload_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.calls += 1
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> dict[str, Any]:
            assert self.calls == 1
            return {"total": 1, "feature_total": 1}

        def fetchall(self) -> list[dict[str, Any]]:
            assert self.calls == 2
            assert "ST_AsGeoJSON(geom)::json AS geometry" in self.statement
            return [
                {
                    "river_segment_id": "seg_huge_props",
                    "river_network_version_id": "rivnet_v01",
                    "basin_version_id": "basin_v01",
                    "segment_order": 1,
                    "downstream_segment_id": None,
                    "length_m": 1000,
                    "properties_json": {"name": "Huge", "blob": "x" * RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES},
                    "geometry": {"type": "LineString", "coordinates": [[91.0, 26.0], [92.0, 27.0]]},
                }
            ]

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

    with pytest.raises(RiverSegmentGeoJsonBudgetError) as exc:
        store.list_river_segments(
            basin_version_id="basin_v01",
            river_network_version_id="rivnet_v01",
            limit=100,
            offset=0,
        )

    assert exc.value.limit_type == "serialized_bytes"
    assert exc.value.scope == "collection"
    assert exc.value.max_bytes == RIVER_SEGMENT_COLLECTION_MAX_SERIALIZED_BYTES


def test_get_river_segment_excludes_null_geometry_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> None:
            assert "WHERE geom IS NOT NULL" in self.statement
            assert self.parameters == ("basin_v01", "seg_null", "rivnet_v01", 10_000, 3)
            return None

    class FakeTransaction:
        def __enter__(self) -> FakeCursor:
            return FakeCursor()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(MissingResourceError, match="renderable geometry"):
        store.get_river_segment(
            basin_version_id="basin_v01",
            river_network_version_id="rivnet_v01",
            segment_id="seg_null",
        )


def test_get_river_segment_excludes_oversized_detail_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> None:
            assert "ST_NPoints(rs.geom) AS coordinate_count" in self.statement
            assert "coordinate_count BETWEEN 2 AND %s" in self.statement
            assert "coordinate_dimensions <= %s" in self.statement
            assert self.parameters == ("basin_v01", "seg_huge", "rivnet_v01", 10_000, 3)
            return None

    class FakeTransaction:
        def __enter__(self) -> FakeCursor:
            return FakeCursor()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(MissingResourceError, match="renderable geometry"):
        store.get_river_segment(
            basin_version_id="basin_v01",
            river_network_version_id="rivnet_v01",
            segment_id="seg_huge",
        )


def test_get_river_segment_rejects_serialized_payload_over_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCursor:
        def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
            self.statement = statement
            self.parameters = parameters

        def fetchone(self) -> dict[str, Any]:
            assert "ST_AsGeoJSON(geom)::json AS geom" in self.statement
            assert self.parameters == ("basin_v01", "seg_huge_props", "rivnet_v01", 10_000, 3)
            return {
                "river_segment_id": "seg_huge_props",
                "river_network_version_id": "rivnet_v01",
                "segment_order": 1,
                "downstream_segment_id": None,
                "length_m": 1000,
                "geom": {"type": "LineString", "coordinates": [[91.0, 26.0], [92.0, 27.0]]},
                "properties_json": {"name": "Huge", "blob": "x" * RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES},
                "created_at": "2026-05-19T00:00:00Z",
            }

    class FakeTransaction:
        def __enter__(self) -> FakeCursor:
            return FakeCursor()

        def __exit__(self, *_args: object) -> bool:
            return False

    monkeypatch.setattr(PsycopgModelRegistryStore, "_transaction", lambda _self: FakeTransaction())
    store = PsycopgModelRegistryStore("postgresql://example")

    with pytest.raises(RiverSegmentGeoJsonBudgetError) as exc:
        store.get_river_segment(
            basin_version_id="basin_v01",
            river_network_version_id="rivnet_v01",
            segment_id="seg_huge_props",
        )

    assert exc.value.limit_type == "serialized_bytes"
    assert exc.value.scope == "detail"
    assert exc.value.max_bytes == RIVER_SEGMENT_DETAIL_MAX_SERIALIZED_BYTES


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


def _public_projection_payload(resource_profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_id": "basins_basin_a_shud",
        "basin_version_id": "basins_basin_a_vbasins",
        "river_network_version_id": "basins_basin_a_rivnet_vbasins",
        "mesh_version_id": "basins_basin_a_mesh_vbasins",
        "calibration_version_id": "basins_basin_a_shud_calib_vbasins",
        "shud_code_version": "basins-shud",
        "model_package_uri": "s3://user:pass@nhms/models/volume/data/Basins/package?token=secret#frag",
        "active_flag": False,
        "lifecycle_state": "inactive",
        "resource_profile": resource_profile,
        "created_at": "2026-05-14T00:00:00Z",
        "basin_id": "basins_basin_a",
        "basin_name": "Basin A",
        "segment_count": 2,
    }


def test_public_model_projection_is_scheme_aware_and_redacts_local_paths() -> None:
    resource_profile = {
        "manifest_uri": "s3://user:pass@nhms/archive/volume/data/Basins/model/manifest.json?token=secret#frag",
        "source_uri": "https://user:pass@objects.example.test/models/data/Basins/source.json?sig=x#frag",
        "copied_root_uri": "integration://user:pass@adapter/volume/data/Basins/root?token=secret#frag",
        "memory_uri": "memory://user:pass@cache/data/Basins/model?token=secret#frag",
        "gs_uri": "gs://user:pass@bucket/volume/data/Basins/model?token=secret#frag",
        "az_uri": "az://user:pass@container/data/Basins/model?token=secret#frag",
        "local_path": "/volume/data/nwm/Basins/local-secret",
        "file_uri": "file:///volume/data/nwm/Basins/file-secret?token=secret#frag",
        "windows_path": "C:\\nwm\\Basins\\win-secret",
        "unc_path": "\\\\server\\share\\Basins\\unc-secret",
        "artifact": {
            "sha256": "sha-secret",
            "package_hash": "hash-secret",
            "inventory_digest": "digest-secret",
        },
    }
    detail = sanitize_model_detail_payload(_public_projection_payload(resource_profile))
    page = sanitize_model_list_payload(
        {"items": [_public_projection_payload(resource_profile)], "total": 1, "limit": 10, "offset": 0}
    )
    item = page["items"][0]

    for projected in (detail, item):
        assert projected["model_package_uri"] == "s3://nhms/models/volume/data/Basins/package"
        profile = projected["resource_profile"]
        assert profile["manifest_uri"] == "s3://nhms/archive/volume/data/Basins/model/manifest.json"
        assert profile["source_uri"] == "https://objects.example.test/models/data/Basins/source.json"
        assert profile["copied_root_uri"] == "integration://adapter/volume/data/Basins/root"
        assert profile["memory_uri"] == "memory://cache/data/Basins/model"
        assert profile["gs_uri"] == "gs://bucket/volume/data/Basins/model"
        assert profile["az_uri"] == "az://container/data/Basins/model"
        assert profile["local_path"] is None
        assert profile["file_uri"] is None
        assert profile["windows_path"] is None
        assert profile["unc_path"] is None
        assert profile["artifact"]["sha256"] is None
        assert profile["artifact"]["package_hash"] is None
        assert profile["artifact"]["inventory_digest"] is None
        rendered = json.dumps(projected)
        for token in (
            "user:pass@",
            "token=secret",
            "#frag",
            "local-secret",
            "file-secret",
            "win-secret",
            "unc-secret",
            "sha-secret",
            "hash-secret",
            "digest-secret",
        ):
            assert token not in rendered


def test_public_model_projection_bounds_deep_wide_and_cyclic_metadata() -> None:
    deep: dict[str, Any] = {"source_path": "/volume/data/nwm/Basins/deep-secret"}
    for _ in range(80):
        deep = {"child": deep}
    cyclic: dict[str, Any] = {"label": "cycle-root"}
    cyclic["self"] = cyclic
    resource_profile = {
        "safe_uri": "s3://user:pass@nhms/volume/data/Basins/safe-object?token=secret#frag",
        "deep": deep,
        "wide": {
            f"path_{index}": f"/volume/data/nwm/Basins/wide-secret-{index}" for index in range(6000)
        },
        "cyclic": cyclic,
    }

    detail = sanitize_model_detail_payload(_public_projection_payload(resource_profile))
    page = sanitize_model_list_payload(
        {"items": [_public_projection_payload(resource_profile)], "total": 1, "limit": 10, "offset": 0}
    )

    for projected in (detail, page["items"][0]):
        rendered = json.dumps(projected)
        assert projected["resource_profile"]["safe_uri"] == "s3://nhms/volume/data/Basins/safe-object"
        assert "deep-secret" not in rendered
        assert "wide-secret" not in rendered
        assert "token=secret" not in rendered
        assert "user:pass@" not in rendered


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
    assert item["resource_profile"]["source_path"] is None
    assert item["resource_profile"]["resolved_source_path"] == "//nhms/resolved-source-path"
    assert item["resource_profile"]["nested"] == [
        {"uri": "s3://nhms/nested"},
        {"uri": "//nhms/nested-protocol-relative"},
        "normal string",
    ]
    public_item_json = json.dumps(item)
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
        assert token not in public_item_json


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


def _assert_pre_body_policy_error(
    response: Any,
    *,
    request_id: str,
    code: str,
    action_id: str,
    decision: str,
    target: dict[str, str],
) -> None:
    body = response.json()
    assert response.headers["X-Request-ID"] == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == code
    details = body["error"]["details"]
    policy_decision = details["policy_decision"]
    audit = details["audit_record"]
    assert policy_decision["action_id"] == action_id
    assert policy_decision["decision"] == decision
    assert policy_decision["target_type"] == target["type"]
    assert policy_decision["target_id"] == target["id"]
    assert policy_decision["no_mutation_expected"] is True
    assert audit["request_id"] == request_id
    assert audit["action_id"] == action_id
    assert audit["decision"] == decision
    assert audit["target"] == target


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
