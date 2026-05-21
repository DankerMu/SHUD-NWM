from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
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

    for response in (default_before, inactive_before, all_before, activation, duplicate, default_after, inactive_after):
        assert response.status_code == 200, response.text
    assert missing.status_code == 404

    assert ids["active_model_id"] in _model_ids(default_before.json())
    assert ids["basins_model_id"] not in _model_ids(default_before.json())
    assert ids["basins_model_id"] in _model_ids(inactive_before.json())
    assert {ids["active_model_id"], ids["basins_model_id"]} <= _model_ids(all_before.json())
    assert ids["basins_model_id"] in _model_ids(default_after.json())
    assert ids["basins_model_id"] not in _model_ids(inactive_after.json())

    activated = activation.json()["data"]["model"]
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

    assert len(audit_rows) == 2
    audit = audit_rows[0]
    assert audit["actor"] == "dev-test:model_admin"
    assert audit["actor_role"] == "model_admin"
    assert audit["action"] == "models.activate"
    assert audit["entity_type"] == "model_instance"
    assert audit["entity_id"] == ids["basins_model_id"]
    assert {
        "operation": "activate",
        "outcome": "allowed",
        "basin_version_id": ids["basin_version_id"],
        "river_network_version_id": ids["river_network_version_id"],
        "mesh_version_id": ids["mesh_version_id"],
    }.items() <= audit["details"].items()
    assert audit["details"]["action_id"] == "models.activate"
    assert audit["details"]["decision"] == "allow"
    assert audit["details"]["roles"] == ["model_admin"]
    assert audit["details"]["target"] == {"type": "model_instance", "id": ids["basins_model_id"]}
    assert audit_rows[1]["details"]["outcome"] == "already_current"
    assert audit["details"].get("model_package_uri") in (None, "[redacted]")
    assert "package-sha-it137" not in json.dumps(audit["details"])
    assert "inventory-sha-it137" not in json.dumps(audit["details"])
    assert "token=secret" not in json.dumps(audit["details"])
    assert "user:pass@" not in json.dumps(audit["details"])
    assert missing_audit_count == 0


def test_m18_raw_model_read_responses_redact_sensitive_asset_metadata(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    try:
        with TestClient(app) as client:
            listing = client.get("/api/v1/models", params={"active": "all"})
            detail = client.get(f"/api/v1/models/{ids['unsafe_model_id']}")
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert listing.status_code == 200, listing.text
    assert detail.status_code == 200, detail.text
    rendered = json.dumps({"listing": listing.json(), "detail": detail.json()})
    for token in (
        "/volume/data",
        "/tmp/nhms/private/model-root",
        "C:\\",
        "file://",
        "user:pass@",
        "token=secret",
        "#credential",
        "#frag",
        "package-sha-it137",
        "inventory-sha-it137",
        "checksum-secret",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "hash-secret",
        "digest-secret",
    ):
        assert token not in rendered

    unsafe_detail = detail.json()["data"]
    assert unsafe_detail["model_package_uri"] is not None
    assert unsafe_detail["resource_profile"]["source_path"] is None
    assert unsafe_detail["resource_profile"]["artifact"]["sha256"] is None
    assert unsafe_detail["resource_profile"]["artifact"]["sha1"] is None
    assert unsafe_detail["resource_profile"]["artifact"]["hash"] is None
    assert unsafe_detail["resource_profile"]["artifact"]["digest"] is None


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
            rollback = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["active_model_id"]},
                headers=headers,
            )
            stale_rollback = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
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
    assert blocked_deactivate.json()["data"]["audit_reference"]["log_id"] is not None
    assert rollback.json()["data"]["status"] == "rollback"
    assert rollback.json()["data"]["model"]["model_id"] == ids["active_model_id"]
    assert rollback.json()["data"]["preflight"]["prior_audit_log_id"] is not None
    assert rollback.json()["data"]["audit_reference"]["log_id"] is not None
    assert stale_rollback.json()["data"]["status"] == "blocked"
    assert stale_rollback.json()["data"]["audit_reference"]["log_id"] is not None
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
    rollback_audits = [row for row in audit_rows if row["details"]["outcome"] == "rollback"]
    assert rollback_audits
    assert rollback_audits[0]["details"]["prior_audit_log_id"] is not None
    assert rollback_audits[0]["details"]["preflight"]["prior_audit_log_id"] == (
        rollback_audits[0]["details"]["prior_audit_log_id"]
    )
    assert any(
        row["action"] == "models.deactivate" and row["details"]["operation"] == "deprecate"
        for row in audit_rows
    )
    rendered = json.dumps(audit_rows)
    assert "/tmp/local" not in rendered
    assert "package-sha-it137" not in rendered
    assert "inventory-sha-it137" not in rendered
    assert "token=secret" not in rendered
    assert "user:pass@" not in rendered



def test_m18_lifecycle_blocks_active_removal_invalid_transition_and_unsafe_evidence(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            headers = {"X-User-Role": "model_admin", "X-User-ID": "m18-admin"}
            supersede_active = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "supersede"},
                headers=headers,
            )
            deprecate_active = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "deprecate"},
                headers=headers,
            )
            activate_unsafe = client.post(
                f"/api/v1/models/{ids['unsafe_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            switch_unsafe = client.post(
                f"/api/v1/models/{ids['unsafe_model_id']}/lifecycle",
                json={"operation": "switch_version"},
                headers=headers,
            )
            unsafe_preflight = client.post(
                f"/api/v1/models/{ids['unsafe_model_id']}/preflight",
                json={"operation": "activate", "reason": "/volume/data/nwm/Basins/raw-secret checksum-secret"},
                headers=headers,
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    for response in (supersede_active, deprecate_active, activate_unsafe, switch_unsafe, unsafe_preflight):
        assert response.status_code == 200, response.text

    assert supersede_active.json()["data"]["status"] == "blocked"
    supersede_codes = {item["code"] for item in supersede_active.json()["data"]["preflight"]["blockers"]}
    assert supersede_codes >= {"MISSING_ACTIVE_RISK"}
    assert deprecate_active.json()["data"]["status"] == "blocked"
    deprecate_codes = {item["code"] for item in deprecate_active.json()["data"]["preflight"]["blockers"]}
    assert deprecate_codes >= {"MISSING_ACTIVE_RISK", "INVALID_TRANSITION"}
    assert activate_unsafe.json()["data"]["status"] == "blocked"
    assert switch_unsafe.json()["data"]["status"] == "blocked"
    unsafe_data = unsafe_preflight.json()["data"]
    unsafe_codes = {item["code"] for item in unsafe_data["blockers"]}
    assert unsafe_codes >= {"SOURCE_ROOT_UNSAFE", "PACKAGE_CHECKSUM_UNVERIFIED"}
    rendered = json.dumps(unsafe_data)
    assert "/volume/data/nwm/Basins" not in rendered
    assert "raw-secret" not in rendered
    assert "checksum-secret" not in rendered

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_id, active_flag, lifecycle_state
                FROM core.model_instance
                WHERE model_id IN (%s, %s, %s)
                ORDER BY model_id
                """,
                (ids["active_model_id"], ids["superseded_model_id"], ids["unsafe_model_id"]),
            )
            states = {row["model_id"]: dict(row) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT COUNT(*) AS active_count
                FROM core.model_instance
                WHERE basin_version_id = %s AND active_flag = true AND lifecycle_state = 'active'
                """,
                (ids["basin_version_id"],),
            )
            active_count = int(cursor.fetchone()["active_count"])

    assert states[ids["active_model_id"]]["active_flag"] is True
    assert states[ids["active_model_id"]]["lifecycle_state"] == "active"
    assert states[ids["superseded_model_id"]]["lifecycle_state"] == "superseded"
    assert states[ids["unsafe_model_id"]]["lifecycle_state"] == "inactive"
    assert active_count == 1


def test_m18_rollback_history_is_bound_to_current_active_epoch(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            headers = {"X-User-Role": "model_admin", "X-User-ID": "m18-rollback-admin"}
            activate_b = client.post(
                f"/api/v1/models/{ids['candidate_a_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            deprecate_a = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "deprecate"},
                headers=headers,
            )
            activate_c = client.post(
                f"/api/v1/models/{ids['candidate_b_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            activate_a = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            stale_rollback = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["candidate_a_model_id"]},
                headers=headers,
            )
            stale_preflight = client.post(
                f"/api/v1/models/{ids['active_model_id']}/preflight",
                json={"operation": "rollback_version", "previous_model_id": ids["candidate_a_model_id"]},
                headers=headers,
            )
            with psycopg_connection(integration_database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT model_id, active_flag, lifecycle_state
                        FROM core.model_instance
                        WHERE model_id IN (%s, %s, %s)
                        ORDER BY model_id
                        """,
                        (ids["active_model_id"], ids["candidate_a_model_id"], ids["candidate_b_model_id"]),
                    )
                    after_stale = {row["model_id"]: dict(row) for row in cursor.fetchall()}
            allowed_preflight = client.post(
                f"/api/v1/models/{ids['active_model_id']}/preflight",
                json={"operation": "rollback_version", "previous_model_id": ids["candidate_b_model_id"]},
                headers=headers,
            )
            allowed_rollback = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["candidate_b_model_id"]},
                headers=headers,
            )
            retry_preflight = client.post(
                f"/api/v1/models/{ids['active_model_id']}/preflight",
                json={"operation": "rollback_version", "previous_model_id": ids["candidate_b_model_id"]},
                headers=headers,
            )
            retry_rollback = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["candidate_b_model_id"]},
                headers=headers,
            )
            deprecate_rolled_back_from = client.post(
                f"/api/v1/models/{ids['active_model_id']}/lifecycle",
                json={"operation": "deprecate", "previous_model_id": ids["candidate_b_model_id"]},
                headers=headers,
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    for response in (
        activate_b,
        deprecate_a,
        activate_c,
        activate_a,
        stale_rollback,
        stale_preflight,
        allowed_preflight,
        allowed_rollback,
        retry_preflight,
        retry_rollback,
        deprecate_rolled_back_from,
    ):
        assert response.status_code == 200, response.text

    assert activate_b.json()["data"]["status"] == "allowed"
    assert deprecate_a.json()["data"]["status"] == "allowed"
    assert activate_c.json()["data"]["status"] == "allowed"
    assert activate_a.json()["data"]["status"] == "allowed"
    assert stale_rollback.json()["data"]["status"] == "blocked"
    stale_codes = {item["code"] for item in stale_rollback.json()["data"]["preflight"]["blockers"]}
    assert stale_codes >= {"ROLLBACK_CURRENT_STALE"}
    stale_rollback_audit_log_id = stale_rollback.json()["data"]["audit_reference"]["log_id"]
    assert stale_rollback_audit_log_id is not None
    assert stale_preflight.json()["data"]["status"] == "blocked"
    stale_preflight_codes = {item["code"] for item in stale_preflight.json()["data"]["blockers"]}
    assert stale_preflight_codes >= {"ROLLBACK_CURRENT_STALE"}
    assert allowed_preflight.json()["data"]["status"] == "ready"
    assert allowed_preflight.json()["data"]["previous_model_id"] == ids["candidate_b_model_id"]
    assert allowed_preflight.json()["data"]["restored_model_id"] == ids["candidate_b_model_id"]

    assert after_stale[ids["active_model_id"]]["active_flag"] is True
    assert after_stale[ids["active_model_id"]]["lifecycle_state"] == "active"
    assert after_stale[ids["candidate_a_model_id"]]["active_flag"] is False
    assert after_stale[ids["candidate_b_model_id"]]["active_flag"] is False

    allowed_data = allowed_rollback.json()["data"]
    assert allowed_data["status"] == "rollback"
    assert allowed_data["model"]["model_id"] == ids["candidate_b_model_id"]
    assert allowed_data["preflight"]["prior_audit_log_id"] is not None
    assert allowed_data["preflight"]["rollback_history"]["matched_previous_model_id"] == ids["candidate_b_model_id"]
    assert allowed_data["preflight"]["rollback_history"]["trusted"] is True
    retry_preflight_data = retry_preflight.json()["data"]
    assert retry_preflight_data["status"] == "ready"
    assert {item["code"] for item in retry_preflight_data["blockers"]} == set()
    assert {item["code"] for item in retry_preflight_data["warnings"]} >= {"ROLLBACK_ALREADY_CURRENT"}
    assert retry_preflight_data["rollback_history"]["trusted"] is True
    retry_data = retry_rollback.json()["data"]
    assert retry_data["status"] == "already_current"
    assert retry_data["model"]["model_id"] == ids["candidate_b_model_id"]
    assert retry_data["previous_model"]["model_id"] == ids["active_model_id"]
    assert retry_data["audit_reference"] is None
    assert retry_data["preflight"]["rollback_history"]["trusted"] is True
    assert {item["code"] for item in retry_data["preflight"]["blockers"]} == set()
    deprecate_data = deprecate_rolled_back_from.json()["data"]
    assert deprecate_data["status"] == "allowed"
    assert deprecate_data["operation"] == "deprecate"
    assert deprecate_data["model"]["model_id"] == ids["active_model_id"]
    assert deprecate_data["model"]["lifecycle_state"] == "deprecated"
    assert deprecate_data["audit_reference"]["log_id"] is not None

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_id, active_flag, lifecycle_state
                FROM core.model_instance
                WHERE model_id IN (%s, %s, %s)
                ORDER BY model_id
                """,
                (ids["active_model_id"], ids["candidate_a_model_id"], ids["candidate_b_model_id"]),
            )
            final_states = {row["model_id"]: dict(row) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT log_id, details
                FROM ops.audit_log
                WHERE entity_id = %s
                  AND details->>'operation' = 'rollback_version'
                  AND details->>'outcome' IN ('rollback', 'blocked')
                ORDER BY created_at DESC, log_id DESC
                """,
                (ids["active_model_id"],),
            )
            rollback_audit_rows = [dict(row) for row in cursor.fetchall()]

    assert final_states[ids["active_model_id"]]["active_flag"] is False
    assert final_states[ids["active_model_id"]]["lifecycle_state"] == "deprecated"
    assert final_states[ids["candidate_a_model_id"]]["active_flag"] is False
    assert final_states[ids["candidate_b_model_id"]]["active_flag"] is True
    assert final_states[ids["candidate_b_model_id"]]["lifecycle_state"] == "active"
    rollback_audit = next(row for row in rollback_audit_rows if row["details"]["outcome"] == "rollback")
    assert rollback_audit["details"]["prior_audit_log_id"] == allowed_data["preflight"]["prior_audit_log_id"]
    assert rollback_audit["details"]["preflight"]["prior_audit_log_id"] == (
        allowed_data["preflight"]["prior_audit_log_id"]
    )
    blocked_rollback_audit = next(
        row for row in rollback_audit_rows if row["log_id"] == stale_rollback_audit_log_id
    )
    assert blocked_rollback_audit["details"]["outcome"] == "blocked"
    assert blocked_rollback_audit["details"]["previous_model"]["model_id"] == ids["active_model_id"]
    assert blocked_rollback_audit["details"]["updated_model"]["model_id"] == ids["active_model_id"]
    assert blocked_rollback_audit["details"]["preflight"]["previous_model_id"] == ids["candidate_a_model_id"]
    assert {item["code"] for item in blocked_rollback_audit["details"]["preflight"]["blockers"]} >= {
        "ROLLBACK_CURRENT_STALE"
    }


def test_m18_rollback_blocks_when_restored_model_safety_evidence_is_unsafe(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            headers = {"X-User-Role": "model_admin", "X-User-ID": "m18-rollback-admin"}
            activate_candidate = client.post(
                f"/api/v1/models/{ids['candidate_a_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            with psycopg_connection(integration_database_url) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE core.model_instance
                        SET model_package_uri = 'ftp://example/private/package',
                            resource_profile = %s
                        WHERE model_id = %s
                        """,
                        (
                            Json(
                                {
                                    "package_checksum": "unsafe-restored-sha",
                                    "package_checksum_confirmed_from_stored_manifest": False,
                                    "source_path": "/tmp/nhms/private/model-root",
                                }
                            ),
                            ids["active_model_id"],
                        ),
                    )
            blocked_rollback = client.post(
                f"/api/v1/models/{ids['candidate_a_model_id']}/lifecycle",
                json={"operation": "rollback_version", "previous_model_id": ids["active_model_id"]},
                headers=headers,
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    assert activate_candidate.status_code == 200, activate_candidate.text
    assert activate_candidate.json()["data"]["status"] == "allowed"
    assert blocked_rollback.status_code == 200, blocked_rollback.text
    data = blocked_rollback.json()["data"]
    assert data["status"] == "blocked"
    blocker_codes = {item["code"] for item in data["preflight"]["blockers"]}
    assert blocker_codes >= {"OBJECT_URI_PREFIX_INVALID", "PACKAGE_CHECKSUM_UNVERIFIED", "SOURCE_ROOT_UNSAFE"}
    assert data["preflight"]["restored_model_id"] == ids["active_model_id"]
    assert data["audit_reference"]["log_id"] is not None

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_id, active_flag, lifecycle_state
                FROM core.model_instance
                WHERE model_id IN (%s, %s)
                ORDER BY model_id
                """,
                (ids["active_model_id"], ids["candidate_a_model_id"]),
            )
            states = {row["model_id"]: dict(row) for row in cursor.fetchall()}

    assert states[ids["candidate_a_model_id"]]["active_flag"] is True
    assert states[ids["candidate_a_model_id"]]["lifecycle_state"] == "active"
    assert states[ids["active_model_id"]]["active_flag"] is False
    assert states[ids["active_model_id"]]["lifecycle_state"] == "superseded"


def test_m18_lifecycle_audit_insert_failure_rolls_back_mutation(integration_database_url: str) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE ops.audit_log ADD CONSTRAINT it137_audit_actor_block CHECK (actor <> 'audit-fail')"
            )
    try:
        with TestClient(app) as client:
            headers = {"X-User-Role": "model_admin", "X-User-ID": "audit-fail"}
            lifecycle_response = client.post(
                f"/api/v1/models/{ids['basins_model_id']}/lifecycle",
                json={"operation": "activate"},
                headers=headers,
            )
            legacy_response = client.put(
                f"/api/v1/models/{ids['basins_model_id']}/active",
                json={"active": True},
                headers=headers,
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)
        with psycopg_connection(integration_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("ALTER TABLE ops.audit_log DROP CONSTRAINT IF EXISTS it137_audit_actor_block")

    for response in (lifecycle_response, legacy_response):
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "blocked"
        assert data["audit_reference"] is None
        assert data["model"]["model_id"] == ids["basins_model_id"]
        assert {item["code"] for item in data["preflight"]["blockers"]} == {
            "LIFECYCLE_AUDIT_PERSISTENCE_FAILED"
        }

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
                SELECT COUNT(*) AS count
                FROM ops.audit_log
                WHERE actor = 'audit-fail'
                """,
            )
            audit_count = int(cursor.fetchone()["count"])

    assert states[ids["active_model_id"]]["active_flag"] is True
    assert states[ids["active_model_id"]]["lifecycle_state"] == "active"
    assert states[ids["basins_model_id"]]["active_flag"] is False
    assert states[ids["basins_model_id"]]["lifecycle_state"] == "inactive"
    assert audit_count == 0


def test_m18_lifecycle_concurrent_activation_leaves_one_active_model(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"

    def activate(model_id: str) -> tuple[int, dict[str, Any]]:
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/models/{model_id}/lifecycle",
                json={"operation": "activate"},
                headers={"X-User-Role": "model_admin", "X-User-ID": f"m18-{model_id}"},
            )
            return response.status_code, response.json()

    try:
        with psycopg_connection(integration_database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE core.model_instance
                    SET active_flag = false, lifecycle_state = 'inactive'
                    WHERE model_id IN (%s, %s)
                    """,
                    (ids["candidate_a_model_id"], ids["candidate_b_model_id"]),
                )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(activate, [ids["candidate_a_model_id"], ids["candidate_b_model_id"]]))
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    for status_code, body in results:
        assert status_code == 200, body
        assert body["data"]["status"] in {"allowed", "already_current"}

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT model_id, active_flag, lifecycle_state
                FROM core.model_instance
                WHERE basin_version_id = %s
                ORDER BY model_id
                """,
                (ids["basin_version_id"],),
            )
            states = [dict(row) for row in cursor.fetchall()]

    active_rows = [row for row in states if row["active_flag"] and row["lifecycle_state"] == "active"]
    assert len(active_rows) == 1
    assert active_rows[0]["model_id"] in {ids["candidate_a_model_id"], ids["candidate_b_model_id"]}


def test_m18_lifecycle_concurrent_activation_and_deactivation_serialize_without_deadlock(
    integration_database_url: str,
) -> None:
    apply_migrations_from_zero(integration_database_url)
    ids = _seed_issue_137_models(integration_database_url)
    app.dependency_overrides[get_model_registry_store] = lambda: PsycopgModelRegistryStore(integration_database_url)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"

    def operate(path_model_id: str, payload: dict[str, Any], user_id: str) -> tuple[int, dict[str, Any]]:
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/models/{path_model_id}/lifecycle",
                json=payload,
                headers={"X-User-Role": "model_admin", "X-User-ID": user_id},
            )
            return response.status_code, response.json()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda args: operate(*args),
                    [
                        (ids["candidate_a_model_id"], {"operation": "activate"}, "m18-activate"),
                        (ids["active_model_id"], {"operation": "deactivate"}, "m18-deactivate"),
                    ],
                )
            )
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(get_model_registry_store, None)

    for status_code, body in results:
        assert status_code == 200, body
        assert body["data"]["status"] in {"allowed", "blocked", "already_current"}
        if body["data"]["status"] == "blocked":
            assert body["data"]["audit_reference"]["log_id"] is not None

    with psycopg_connection(integration_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS active_count
                FROM core.model_instance
                WHERE basin_version_id = %s
                  AND active_flag = true
                  AND lifecycle_state = 'active'
                """,
                (ids["basin_version_id"],),
            )
            active_count = int(cursor.fetchone()["active_count"])

    assert active_count == 1


def _seed_issue_137_models(database_url: str) -> dict[str, str]:
    ids = {
        "basin_id": "it137_basin",
        "basin_version_id": "it137_basin_v1",
        "river_network_version_id": "it137_rnv_v1",
        "mesh_version_id": "it137_mesh_v1",
        "active_model_id": "it137_active_model",
        "basins_model_id": "it137_basins_model",
        "superseded_model_id": "it137_superseded_model",
        "unsafe_model_id": "it137_unsafe_model",
        "candidate_a_model_id": "it137_candidate_a_model",
        "candidate_b_model_id": "it137_candidate_b_model",
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
                    lifecycle_state,
                    resource_profile
                )
                VALUES
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, true, 'active', %s),
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, false, 'inactive', %s),
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, false, 'inactive', %s),
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, false, 'inactive', %s),
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, false, 'inactive', %s),
                    (%s, %s, %s, %s, 'calib-v1', 'shud-v1', %s, false, 'inactive', %s)
                """,
                (
                    ids["active_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://nhms/models/it137_active_model/package/",
                    Json(
                        {
                            "fixture": "issue-137-active",
                            "package_checksum": "package-sha-active",
                            "manifest_uri": "s3://nhms/models/it137_active_model/v1/manifest.json",
                        }
                    ),
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
                    ids["superseded_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://nhms/models/it137_superseded_model/package/",
                    Json(
                        {
                            "fixture": "issue-137-superseded",
                            "package_checksum": "package-sha-superseded",
                            "manifest_uri": "s3://nhms/models/it137_superseded_model/v1/manifest.json",
                        }
                    ),
                    ids["unsafe_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://nhms/models/it137_unsafe_model/package/",
                    Json({
                        "fixture": "issue-137-unsafe",
                        "package_checksum": "checksum-secret",
                        "manifest_uri": "s3://nhms/models/it137_unsafe_model/v1/manifest.json",
                        "package_checksum_confirmed_from_stored_manifest": False,
                        "source_path": "/tmp/nhms/private/model-root",
                        "resolved_source_path": "/volume/data/nwm/Basins/raw-secret",
                        "artifact": {
                            "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                            "sha1": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                            "hash": "hash-secret",
                            "digest": "digest-secret",
                        },
                        "source_is_symlink": False,
                    }),
                    ids["candidate_a_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://nhms/models/it137_candidate_a_model/package/",
                    Json(
                        {
                            "fixture": "issue-137-candidate-a",
                            "package_checksum": "package-sha-candidate-a",
                            "manifest_uri": "s3://nhms/models/it137_candidate_a_model/v1/manifest.json",
                        }
                    ),
                    ids["candidate_b_model_id"],
                    ids["basin_version_id"],
                    ids["river_network_version_id"],
                    ids["mesh_version_id"],
                    "s3://nhms/models/it137_candidate_b_model/package/",
                    Json(
                        {
                            "fixture": "issue-137-candidate-b",
                            "package_checksum": "package-sha-candidate-b",
                            "manifest_uri": "s3://nhms/models/it137_candidate_b_model/v1/manifest.json",
                        }
                    ),
                ),
            )
            cursor.execute(
                """
                UPDATE core.model_instance
                SET lifecycle_state = 'superseded'
                WHERE model_id = %s
                """,
                (ids["superseded_model_id"],),
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
