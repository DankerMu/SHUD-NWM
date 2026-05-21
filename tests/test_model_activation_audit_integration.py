from __future__ import annotations

import json
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from psycopg2.extras import Json

from apps.api.main import app
from apps.api.routes.models import get_model_registry_store
from packages.common.model_registry import PsycopgModelRegistryStore
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration


def test_basins_model_activation_listing_and_audit_evidence(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            headers = {"X-User-Role": "model_admin"}
            default_before = client.get("/api/v1/models")
            inactive_before = client.get("/api/v1/models", params={"active": "false"})
            all_before = client.get("/api/v1/models", params={"active": "all"})
            activation = client.put(
                f"/api/v1/models/{ids['basins_model_id']}/active",
                json={"active": True},
                headers=headers,
            )
            duplicate = client.put(
                f"/api/v1/models/{ids['basins_model_id']}/active",
                json={"active": True},
                headers=headers,
            )
            missing = client.put("/api/v1/models/it137_missing_model/active", json={"active": True}, headers=headers)
            default_after = client.get("/api/v1/models")
            inactive_after = client.get("/api/v1/models", params={"active": "false"})
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    for response in (default_before, inactive_before, all_before, activation, default_after, inactive_after):
        assert response.status_code == 200, response.text
    assert duplicate.status_code == 409
    assert missing.status_code == 404

    assert ids["active_model_id"] in _model_ids(default_before.json())
    assert ids["basins_model_id"] not in _model_ids(default_before.json())
    assert ids["basins_model_id"] in _model_ids(inactive_before.json())
    assert {ids["active_model_id"], ids["basins_model_id"]} <= _model_ids(all_before.json())
    assert ids["basins_model_id"] in _model_ids(default_after.json())
    assert ids["basins_model_id"] not in _model_ids(inactive_after.json())

    activated = activation.json()["data"]
    assert activated["active_flag"] is True
    assert activated["resource_profile"]["basin_slug"] == "it137-basin"
    assert activated["resource_profile"]["manifest_uri"] == (
        "s3://nhms/models/it137_basins_model/v1/manifest.json"
    )

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT actor, actor_role, action, entity_type, entity_id, details
                FROM ops.audit_log
                WHERE entity_type = 'model_instance'
                  AND entity_id = %s
                ORDER BY created_at, log_id
                """,
                (ids["basins_model_id"],),
            )
            audit_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM ops.audit_log
                WHERE entity_id = 'it137_missing_model'
                """,
            )
            missing_audit_count = int(cursor.fetchone()["count"])

    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit["actor"] == "dev-test:model_admin"
    assert audit["actor_role"] == "model_admin"
    assert audit["action"] == "models.activate"
    assert audit["entity_type"] == "model_instance"
    assert audit["entity_id"] == ids["basins_model_id"]
    assert {
        "previous_active": False,
        "active": True,
        "basin_version_id": ids["basin_version_id"],
        "river_network_version_id": ids["river_network_version_id"],
        "mesh_version_id": ids["mesh_version_id"],
    }.items() <= audit["details"].items()
    assert audit["details"]["action_id"] == "models.activate"
    assert audit["details"]["decision"] == "allow"
    assert audit["details"]["roles"] == ["model_admin"]
    assert audit["details"]["target"] == {"type": "model_instance", "id": ids["basins_model_id"]}
    assert audit["details"].get("model_package_uri") in (None, "[redacted]")
    assert "package-sha-it137" not in json.dumps(audit["details"])
    assert "inventory-sha-it137" not in json.dumps(audit["details"])
    assert "token=secret" not in json.dumps(audit["details"])
    assert "user:pass@" not in json.dumps(audit["details"])
    assert missing_audit_count == 0


def test_m18_lifecycle_activation_switch_rollback_and_redacted_audit(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            headers = {"X-User-Role": "model_admin", "X-User-ID": "m18-admin"}
            activation = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "activate", "reason": "activate from /tmp/local?token=secret"},
                headers=headers,
            )
            repeated = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            blocked_deactivate = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "deactivate"},
                headers=headers,
            )
            override_deactivate = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "deactivate", "override_missing_active": True, "reason": "planned maintenance"},
                headers={"X-User-Role": "sys_admin", "X-User-ID": "m18-root"},
            )
            switch = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "switch_version"},
                headers=headers,
            )
            rollback = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["active_model_id"]},
                headers=headers,
            )
            stale_rollback = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["active_model_id"]},
                headers=headers,
            )
            supersede = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "supersede"},
                headers=headers,
            )
            deprecate = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "deprecate"},
                headers=headers,
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    lifecycle_responses = (
        activation,
        repeated,
        blocked_deactivate,
        override_deactivate,
        switch,
        rollback,
        stale_rollback,
        supersede,
        deprecate,
    )
    for response in lifecycle_responses:
        assert response.status_code == 200, response.text

    assert activation.json()["data"]["status"] == "allowed"
    assert activation.json()["data"]["model"]["lifecycle_state"] == "active"
    assert repeated.json()["data"]["status"] == "already_current"
    assert blocked_deactivate.json()["data"]["status"] == "blocked"
    assert blocked_deactivate.json()["data"]["preflight"]["blockers"][0]["code"] == "MISSING_ACTIVE_RISK"
    assert override_deactivate.json()["data"]["model"]["lifecycle_state"] == "inactive"
    assert switch.json()["data"]["model"]["lifecycle_state"] == "active"
    assert rollback.json()["data"]["status"] == "rollback"
    assert rollback.json()["data"]["model"]["model_id"] == ids["active_model_id"]
    assert stale_rollback.json()["data"]["status"] == "blocked"
    stale_blocker_codes = {item["code"] for item in stale_rollback.json()["data"]["preflight"]["blockers"]}
    assert stale_blocker_codes >= {"ROLLBACK_CURRENT_STALE"}
    assert supersede.json()["data"]["model"]["lifecycle_state"] == "superseded"
    assert deprecate.json()["data"]["model"]["lifecycle_state"] == "deprecated"

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_id, active_flag, lifecycle_state
                FROM core.model_instance
                WHERE model_id IN (%s, %s)
                ORDER BY model_id
                """,
                (ids["active_model_id"], ids["basins_model_id"]),
            )
            states = {row["model_id"]: dict(row) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT action, entity_id, details
                FROM ops.audit_log
                WHERE entity_id IN (%s, %s)
                  AND details ? 'operation'
                ORDER BY created_at, log_id
                """,
                (ids["active_model_id"], ids["basins_model_id"]),
            )
            audit_rows = [dict(row) for row in cursor.fetchall()]

    assert states[ids["active_model_id"]]["active_flag"] is True
    assert states[ids["active_model_id"]]["lifecycle_state"] == "active"
    assert states[ids["basins_model_id"]]["active_flag"] is False
    assert states[ids["basins_model_id"]]["lifecycle_state"] == "deprecated"
    assert {row["details"]["outcome"] for row in audit_rows} >= {"allowed", "already_current", "blocked", "rollback"}
    assert any(
        row["action"] == "models.deactivate" and row["details"]["operation"] == "deprecate"
        for row in audit_rows
    )
    rendered = json.dumps(audit_rows)
    assert "/tmp/local" not in rendered
    assert "planned maintenance" not in rendered
    assert "package-sha-it137" not in rendered
    assert "inventory-sha-it137" not in rendered
    assert "token=secret" not in rendered
    assert "user:pass@" not in rendered


def _seed_issue_137_models(database_url: str) -> dict[str, str]:
    ids = {
        "basin_id": "it137_basin",
        "basin_version_id": "it137_basin_v1",
        "river_network_version_id": "it137_rnv_v1",
        "mesh_version_id": "it137_mesh_v1",
        "active_model_id": "it137_active_model",
        "basins_model_id": "it137_basins_model",
    }
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            _delete_issue_137_rows(cursor)
            cursor.execute(
                """
                INSERT INTO core.basin (basin_id, basin_name, basin_group, description)
                VALUES (%s, 'Issue 137 Basin', 'integration', 'Activation audit fixture.')
                """,
                (ids["basin_id"],),
            )
            cursor.execute(
                """
                INSERT INTO core.basin_version (
                    basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
                )
                VALUES (
                    %s, %s, 'v1', ST_Multi(ST_MakeEnvelope(100.0, 30.0, 101.0, 31.0, 4490)),
                    true, 'integration://it137/basin', 'basin-sha-it137'
                )
                """,
                (ids["basin_version_id"], ids["basin_id"]),
            )
            cursor.execute(
                """
                INSERT INTO core.river_network_version (
                    river_network_version_id, basin_version_id, version_label, segment_count, source_uri, checksum
                )
                VALUES (%s, %s, 'v1', 0, 'integration://it137/river-network', 'rnv-sha-it137')
                """,
                (ids["river_network_version_id"], ids["basin_version_id"]),
            )
            cursor.execute(
                """
                INSERT INTO core.mesh_version (
                    mesh_version_id, basin_version_id, version_label, mesh_uri, checksum, properties_json
                )
                VALUES (%s, %s, 'v1', 's3://nhms/models/it137/mesh', 'mesh-sha-it137', %s)
                """,
                (ids["mesh_version_id"], ids["basin_version_id"], Json({"fixture": "issue-137"})),
            )
            cursor.execute(
                """
                INSERT INTO core.model_instance (
                    model_id,
                    basin_version_id,
                    river_network_version_id,
                    mesh_version_id,
                    calibration_version_id,
                    shud_code_version,
                    model_package_uri,
                    active_flag,
                    resource_profile
                )
                VALUES
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, true, %s),
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, false, %s)
                """,
                (
                    ids["active_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://nhms/models/it137_active_model/package/",
                    Json({"fixture": "issue-137-active"}),
                    ids["basins_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://user:pass@nhms/models/it137_basins_model/package/?token=secret#credential",
                    Json(
                        {
                            "fixture": "issue-137-basins",
                            "basin_slug": "it137-basin",
                            "shud_input_name": "it137_basin",
                            "manifest_uri": (
                                "s3://user:pass@nhms/models/it137_basins_model/v1/manifest.json"
                                "?token=secret#credential"
                            ),
                            "package_checksum": "package-sha-it137",
                            "source_inventory_checksum": "inventory-sha-it137",
                        }
                    ),
                ),
            )
    return ids


def _delete_issue_137_rows(cursor: Any) -> None:
    cursor.execute("DELETE FROM ops.audit_log WHERE entity_id LIKE 'it137_%'")
    cursor.execute("DELETE FROM core.model_instance WHERE model_id LIKE 'it137_%'")
    cursor.execute("DELETE FROM core.mesh_version WHERE mesh_version_id = 'it137_mesh_v1'")
    cursor.execute("DELETE FROM core.river_network_version WHERE river_network_version_id = 'it137_rnv_v1'")
    cursor.execute("DELETE FROM core.basin_version WHERE basin_version_id = 'it137_basin_v1'")
    cursor.execute("DELETE FROM core.basin WHERE basin_id = 'it137_basin'")


def _model_ids(body: dict[str, Any]) -> set[str]:
    return {item["model_id"] for item in body["data"]["items"]}


def _has_no_sensitive_uri_parts(value: str) -> bool:
    return "token=" not in value and "?" not in value and "#" not in value and "user:pass@" not in value
