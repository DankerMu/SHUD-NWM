from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from packages.common import safe_fs
from services.production_closure import ops_validation as ops_validation_module
from services.production_closure import slurm_validation
from services.production_closure.ops_validation import (
    MAX_EVIDENCE_PAYLOAD_BYTES,
    MAX_PERCENT_DECODE_ROUNDS,
    EvidenceWriter,
    ProductionOpsConfig,
    ProductionOpsValidationError,
    _argparse_main,
    validate_ops,
)


def test_validate_ops_default_lane_writes_required_release_blocked_evidence(tmp_path: Path) -> None:
    summary = validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m10_152"))

    lane_dir = tmp_path / "artifacts" / "m10_152" / "ops"
    assert summary["schema"] == "nhms.production_closure.ops.v1"
    assert summary["status"] == "release_blocked"
    assert summary["final_production_readiness_claimed"] is False
    assert summary["evidence_dir"] == "m10_152/ops"
    assert summary["live_backend_auth_executed"] is False
    assert summary["live_alert_sink_delivered"] is False
    assert summary["live_rollback_executed"] is False
    assert summary["dependency_status"] == "release_blocked"
    assert summary["files"] == [
        "preflight.json",
        "config_validation.json",
        "auth_rbac.json",
        "auth_release_blockers.json",
        "audit_redaction.json",
        "monitoring_alerts.json",
        "rollback_drills.json",
        "dependency_closure.json",
        "environment.json",
        "summary.json",
    ]
    for name in summary["files"]:
        assert (lane_dir / name).is_file()

    preflight = _read_json(lane_dir / "preflight.json")
    assert preflight["auth_mode"] == "fallback_release_gated"
    assert set(preflight["required_roles"]) >= {"viewer", "analyst", "operator", "model_admin", "sys_admin"}
    assert preflight["alert_target"] == "dry-run://ops-validation"
    assert preflight["deployment_config_source"] == "generated_deterministic_templates"
    assert preflight["rollback_drill_scope"] == "simulated_drills"
    assert preflight["evidence_dir"] == "m10_152/ops"
    assert set(preflight["dependency_evidence"]) == {"slurm", "object_store", "met", "e2e", "scale"}
    assert preflight["execution_policy"] == {
        "default_fast_path": "deterministic_fixture",
        "real_identity_provider_required": False,
        "external_material_required": False,
        "alert_sink_required": False,
        "object_store_required": False,
        "slurm_required": False,
        "postgis_api_frontend_required": False,
        "scheduler_required": False,
        "final_readiness_requires_live_controls_and_accepted_dependencies": True,
    }

    config = _read_json(lane_dir / "config_validation.json")
    assert {item["service"] for item in config["services"]} == {
        "api",
        "orchestrator",
        "slurm_gateway",
        "tile_publisher",
        "frontend",
        "database",
        "object_store",
        "source_adapters",
        "workspace_roots",
    }
    assert config["status"] == "blocked"
    assert all(item["required_settings"] for item in config["services"])
    assert any(blocker["error_code"] == "PRODUCTION_OPS_CONFIG_UNSAFE_SETTING" for blocker in config["blockers"])
    assert any(blocker["error_code"] == "PRODUCTION_OPS_CONFIG_MISSING_SETTING" for blocker in config["blockers"])
    for service in config["services"]:
        assert Path(service["template_reference"]).is_file()
        assert {item["setting"] for item in service["setting_source_metadata"]} == set(service["required_settings"])
    blockers_by_setting = {
        (blocker["service"], blocker["setting"])
        for blocker in config["blockers"]
        if blocker["error_code"] == "PRODUCTION_OPS_CONFIG_MISSING_SETTING"
    }
    for service in config["services"]:
        for setting in service["required_settings"]:
            assert (service["service"], setting) in blockers_by_setting
            metadata = next(item for item in service["setting_source_metadata"] if item["setting"] == setting)
            assert metadata["source"] == "generated_default"
            assert metadata["missing_required"] is True


def test_validate_ops_auth_rbac_audit_and_release_blockers_are_complete(tmp_path: Path) -> None:
    validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="auth"))
    lane_dir = tmp_path / "artifacts" / "auth" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    blockers = _read_json(lane_dir / "auth_release_blockers.json")
    audit = _read_json(lane_dir / "audit_redaction.json")

    expected_actions = {
        "pipeline.retry_run",
        "pipeline.cancel_run",
        "pipeline.rerun_cycle",
        "qc.override_result",
        "tiles.republish",
        "sources.update_config",
        "models.activate",
        "models.deactivate",
        "models.switch_version",
        "models.rollback_version",
        "models.supersede",
        "users.manage",
    }
    assert set(auth["canonical_action_ids"]) == expected_actions
    assert set(auth["canonical_roles"]) == {"viewer", "analyst", "operator", "model_admin", "sys_admin"}
    assert {item["action_id"] for item in auth["action_decisions"]} == expected_actions
    assert {item["decision"] for item in auth["action_decisions"]} == {
        "allow",
        "deny",
        "release_blocked",
    }
    assert set(auth["execution_modes"]) == {"policy_simulated", "release_blocked"}
    assert auth["live_backend_auth_executed"] is False
    assert auth["state_mutation_assertions"] == {
        "denied_actions_mutated_state": False,
        "release_blocked_actions_mutated_state": False,
    }
    for decision in auth["action_decisions"]:
        if decision["decision"] in {"denied", "deny", "release_blocked"}:
            assert decision["previous_state"] == decision["new_state"]
            assert decision["state_mutated"] is False
            assert decision["no_mutation_expected"] is True
            assert decision["error_code"] in {
                "PRODUCTION_OPS_AUTH_REQUIRED",
                "PRODUCTION_OPS_RBAC_FORBIDDEN",
                "PRODUCTION_OPS_BACKEND_AUTH_RELEASE_BLOCKED",
            }

    assert blockers["status"] == "release_blocked"
    assert {item["action_id"] for item in blockers["blockers"]} == expected_actions
    assert all(item["residual_risk"] and item["removal_criteria"] for item in blockers["blockers"])

    assert audit["status"] == "ready"
    assert len(audit["audit_rows"]) == len(auth["action_decisions"])
    assert set(audit["redaction_scope"]) == {
        "config",
        "logs",
        "manifests",
        "audit_rows",
        "api_payloads",
        "alert_payloads",
        "pr_evidence",
        "frontend_output",
    }
    first_row = audit["audit_rows"][0]
    assert {
        "actor",
        "actor_id",
        "roles",
        "action",
        "action_id",
        "target",
        "previous_state",
        "new_state",
        "decision",
        "reason",
        "reason_code",
        "execution_mode",
        "lineage",
    } <= set(first_row)
    assert {row["decision"] for row in audit["audit_rows"]} == {"allow", "deny", "release_blocked"}
    represented_surfaces = {
        "config": "config",
        "logs": "log_output",
        "manifests": "manifest_payload",
        "audit_rows": "audit_correlation_id",
        "api_payloads": "api_payload",
        "alert_payloads": "alert_payload",
        "pr_evidence": "pr_evidence",
        "frontend_output": "frontend_output",
    }
    first_lineage = first_row["lineage"]
    for field in represented_surfaces.values():
        assert field in first_lineage
    audit_text = json.dumps(audit)
    for surface, field in represented_surfaces.items():
        assert surface in audit["redaction_scope"]
        if surface != "audit_rows":
            assert "[redacted]" in json.dumps(first_lineage[field])
    assert "deterministic-secret-for-redaction-test" not in audit_text
    assert "deterministic-secret" not in audit_text


def test_validate_ops_summary_redacts_absolute_evidence_path(tmp_path: Path) -> None:
    evidence_root = tmp_path / "absolute-evidence-root"
    validate_ops(ProductionOpsConfig.from_env(evidence_root=evidence_root, run_id="redacted_path"))

    lane_dir = evidence_root / "redacted_path" / "ops"
    summary = _read_json(lane_dir / "summary.json")
    environment = _read_json(lane_dir / "environment.json")
    auth = _read_json(lane_dir / "auth_rbac.json")
    rendered = json.dumps({"summary": summary, "environment": environment, "auth": auth})

    assert summary["evidence_dir"] == "redacted_path/ops"
    assert str(evidence_root) not in rendered
    assert str(lane_dir) not in rendered


def test_validate_ops_monitoring_alerts_and_rollback_drills_cover_required_surfaces(tmp_path: Path) -> None:
    validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="runbooks"))
    lane_dir = tmp_path / "artifacts" / "runbooks" / "ops"
    alerts = _read_json(lane_dir / "monitoring_alerts.json")
    rollback = _read_json(lane_dir / "rollback_drills.json")

    assert {item["alert"] for item in alerts["alerts"]} == {
        "source_latency",
        "slurm_queue_backlog",
        "failed_basin_retries",
        "object_store_failure",
        "stale_analysis_state",
        "tile_error",
        "api_p95",
    }
    assert alerts["status"] == "release_blocked"
    assert alerts["live_alert_sink_delivered"] is False
    for alert in alerts["alerts"]:
        assert alert["execution_mode"] == "dry_run_sink"
        assert alert["live_alert_sink_delivered"] is False
        assert alert["dry_run_target"] == "dry-run://ops-validation"
        assert alert["metric"]
        assert alert["severity"] in {"warning", "critical"}
        assert isinstance(alert["observed_value"], float)
        assert isinstance(alert["threshold"], float)
        assert alert["runbook_link"].startswith("docs/runbooks/")
        assert Path(alert["runbook_link"]).is_file()
        assert alert["recommended_operator_action"]

    assert {item["drill"] for item in rollback["drills"]} == {
        "bad_model_activation",
        "failed_publish_import",
        "failed_source_cycle",
        "failed_slurm_array",
        "bad_tile_release",
    }
    assert rollback["status"] == "release_blocked"
    assert rollback["live_rollback_executed"] is False
    for drill in rollback["drills"]:
        assert drill["execution_mode"] == "simulated_drill"
        assert drill["live_rollback_executed"] is False
        assert drill["command"]
        assert drill["precondition"]
        assert drill["expected_evidence"]
        assert drill["recovery_result"]
        assert drill["residual_risk"]
        assert drill["dependency_artifact_references"]
        assert Path(drill["runbook_link"]).is_file()


def test_validate_ops_dependency_closure_accepts_real_summaries_but_keeps_live_control_gate(
    tmp_path: Path,
) -> None:
    roots: dict[str, Path] = {}
    for name, issue, schema, status in [
        ("slurm", 147, "nhms.production_closure.slurm.v1", "submitted"),
        ("object_store", 148, "nhms.production_closure.object_store.v1", "ready"),
        ("met", 149, "nhms.production_closure.met.v1", "ready"),
        ("e2e", 150, "nhms.production_closure.e2e.v1", "ready"),
        ("scale", 151, "nhms.production_closure.scale.v1", "ready"),
    ]:
        root = tmp_path / name
        root.mkdir()
        _write_dependency_summary(root / "summary.json", name, issue, schema, status, accepted=True)
        roots[name] = root

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="accepted_deps",
            slurm_evidence_root=roots["slurm"],
            object_store_evidence_root=roots["object_store"],
            met_evidence_root=roots["met"],
            e2e_evidence_root=roots["e2e"],
            scale_evidence_root=roots["scale"],
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "accepted_deps" / "ops" / "dependency_closure.json")
    assert dependency["status"] == "accepted"
    assert {item["status"] for item in dependency["dependencies"]} == {"accepted"}
    assert dependency["blockers"] == []
    assert dependency["final_production_readiness_claimed"] is False
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "accepted"
    assert summary["final_production_readiness_claimed"] is False


def test_validate_ops_dependency_closure_requires_external_acceptance_receipt_for_producer_summary(
    tmp_path: Path,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "final_production_readiness_claimed": False,
            "live_slurm_executed": True,
            "live_slurm_status": "executed",
        },
    )

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="producer_without_receipt",
            slurm_evidence_root=root,
        )
    )
    dependency = _read_json(tmp_path / "artifacts" / "producer_without_receipt" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert "accepted_dependency_evidence.json" in slurm["reason"]

    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")
    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="producer_with_receipt",
            slurm_evidence_root=root,
        )
    )
    accepted_dependency = _read_json(
        tmp_path / "artifacts" / "producer_with_receipt" / "ops" / "dependency_closure.json"
    )
    accepted_slurm = next(item for item in accepted_dependency["dependencies"] if item["dependency"] == "slurm")
    assert accepted_slurm["status"] == "accepted"
    assert accepted_slurm["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()
    assert accepted_slurm["accepted_dependency_evidence"]["receipt_path"] == str(
        root / "accepted_dependency_evidence.json"
    )


def test_validate_ops_accepts_object_store_fast_summary_with_live_proof_blocker(tmp_path: Path) -> None:
    root = tmp_path / "object_store"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
        extra={
            "execution_mode": "deterministic_fixture",
            "deterministic_fixture": True,
            "live_registry_import": False,
            "live_api": False,
            "live_api_status": "not_executed",
            "api_contract_source": "local_import_source",
            "final_production_readiness_claimed": False,
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "object_store", 148, "nhms.production_closure.object_store.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="object_store_fast_with_receipt",
            object_store_evidence_root=root,
        )
    )

    dependency = _read_json(
        tmp_path / "artifacts" / "object_store_fast_with_receipt" / "ops" / "dependency_closure.json"
    )
    object_store = next(item for item in dependency["dependencies"] if item["dependency"] == "object_store")
    assert object_store["status"] == "accepted"
    assert object_store["deterministic_fixture"] is False
    assert object_store["summary_deterministic_fixture"] is True
    assert object_store["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()
    assert object_store["release_blockers"][0]["error_code"] == (
        "PRODUCTION_OPS_DEPENDENCY_PRODUCER_LIVE_PROOF_MISSING"
    )
    assert dependency["status"] == "release_blocked"


def test_validate_ops_accepts_object_store_only_with_summary_live_proof_and_receipt(tmp_path: Path) -> None:
    root = tmp_path / "object_store"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "live_registry_import": True,
            "live_api": True,
            "live_api_status": "executed",
            "final_production_readiness_claimed": False,
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "object_store", 148, "nhms.production_closure.object_store.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="object_store_live_with_receipt",
            object_store_evidence_root=root,
        )
    )

    dependency = _read_json(
        tmp_path / "artifacts" / "object_store_live_with_receipt" / "ops" / "dependency_closure.json"
    )
    object_store = next(item for item in dependency["dependencies"] if item["dependency"] == "object_store")
    assert object_store["status"] == "accepted"
    assert object_store["deterministic_fixture"] is False
    assert object_store["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()


@pytest.mark.parametrize(
    ("name", "issue", "schema", "status", "extra"),
    [
        (
            "object_store",
            148,
            "nhms.production_closure.object_store.v1",
            "ready",
            {
                "execution_mode": "deterministic_fixture",
                "deterministic_fixture": True,
                "live_registry_import": False,
                "live_api": False,
                "live_api_status": "not_executed",
                "final_production_readiness_claimed": False,
            },
        ),
        (
            "e2e",
            150,
            "nhms.production_closure.e2e.v1",
            "ready",
            {
                "execution_mode": "deterministic_fixture",
                "deterministic_fixture": True,
                "live_db_executed": False,
                "live_api_executed": False,
                "live_slurm_executed": False,
                "live_frontend_executed": False,
                "final_production_readiness_claimed": False,
            },
        ),
        (
            "scale",
            151,
            "nhms.production_closure.scale.v1",
            "ready",
            {
                "execution_mode": "deterministic_fixture",
                "deterministic_fixture": True,
                "live_db_executed": False,
                "live_api_executed": False,
                "live_frontend_executed": False,
                "final_production_readiness_claimed": False,
            },
        ),
    ],
)
def test_validate_ops_consumes_real_producer_summary_shapes_with_receipt_as_accepted_blocked(
    tmp_path: Path,
    name: str,
    issue: int,
    schema: str,
    status: str,
    extra: dict,
) -> None:
    root = tmp_path / name
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, name, issue, schema, status, extra=extra)
    _write_dependency_acceptance_receipt(summary_path, name, issue, schema)

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id=f"{name}_real_shape_with_receipt",
            object_store_evidence_root=root if name == "object_store" else None,
            e2e_evidence_root=root if name == "e2e" else None,
            scale_evidence_root=root if name == "scale" else None,
        )
    )

    dependency = _read_json(
        tmp_path / "artifacts" / f"{name}_real_shape_with_receipt" / "ops" / "dependency_closure.json"
    )
    item = next(item for item in dependency["dependencies"] if item["dependency"] == name)
    assert item["status"] == "accepted"
    assert item["summary_deterministic_fixture"] is True
    assert item["accepted_dependency_evidence"]["summary_sha256"] == hashlib.sha256(
        summary_path.read_bytes()
    ).hexdigest()
    assert item["release_blockers"][0]["error_code"] == "PRODUCTION_OPS_DEPENDENCY_PRODUCER_LIVE_PROOF_MISSING"
    assert dependency["status"] == "release_blocked"


def test_validate_ops_accepts_live_dependency_with_unrelated_false_live_fields(tmp_path: Path) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
        extra={
            "live_alert_sink_delivered": False,
            "live_frontend_executed": False,
            "live_registry_import": False,
            "live_api": False,
            "live_api_status": "not_executed",
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="accepted_live_with_unrelated_false_fields",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(
        tmp_path
        / "artifacts"
        / "accepted_live_with_unrelated_false_fields"
        / "ops"
        / "dependency_closure.json"
    )
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert slurm["deterministic_fixture"] is False


def test_validate_ops_rejects_spoofed_live_field_even_with_receipt(tmp_path: Path) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "final_production_readiness_claimed": False,
            "live_spoof": True,
        },
    )
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="spoofed_live_field",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "spoofed_live_field" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert slurm["deterministic_fixture"] is False
    assert slurm["release_blockers"][0]["error_code"] == "PRODUCTION_OPS_DEPENDENCY_PRODUCER_LIVE_PROOF_MISSING"


def test_validate_ops_dependency_receipt_uses_bounded_summary_digest_without_second_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra={
            "execution_mode": "accepted_live_evidence",
            "deterministic_fixture": False,
            "final_production_readiness_claimed": False,
            "live_slurm_executed": True,
            "live_slurm_status": "executed",
        },
    )
    summary_bytes = summary_path.read_bytes()
    expected_digest = hashlib.sha256(summary_bytes).hexdigest()
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    original_read_bytes = Path.read_bytes

    def fail_summary_read_bytes(path: Path) -> bytes:
        if path == summary_path.resolve():
            raise AssertionError("summary.json must not be re-read for receipt checksum validation")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_summary_read_bytes)

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="producer_receipt_single_read_digest",
            slurm_evidence_root=root,
        )
    )
    dependency = _read_json(
        tmp_path / "artifacts" / "producer_receipt_single_read_digest" / "ops" / "dependency_closure.json"
    )
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert slurm["accepted_dependency_evidence"]["summary_sha256"] == expected_digest


@pytest.mark.parametrize(
    ("summary_fields", "expected_status"),
    [
        ({"deterministic_fixture": True}, "skipped"),
        ({"execution_mode": "deterministic_fixture"}, "skipped"),
        ({"live_slurm_executed": False}, "skipped"),
        ({}, "blocked"),
        (
            {
                "accepted_dependency_evidence": {
                    "accepted": True,
                    "receipt_id": "missing-fields",
                    "execution_mode": "accepted_live_evidence",
                    "deterministic_fixture": False,
                    "final_production_readiness_claimed": False,
                }
            },
            "blocked",
        ),
    ],
)
def test_validate_ops_dependency_closure_rejects_unproven_ready_summaries(
    tmp_path: Path,
    summary_fields: dict,
    expected_status: str,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    _write_dependency_summary(
        root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        extra=summary_fields,
    )

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="unproven_deps",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "unproven_deps" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == expected_status
    assert slurm["final_production_readiness_claimed"] is False
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert dependency["status"] == "release_blocked"
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"


def test_validate_ops_dependency_closure_rejects_final_readiness_claimed_summary(tmp_path: Path) -> None:
    root = tmp_path / "object_store"
    root.mkdir()
    _write_dependency_summary(
        root / "summary.json",
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
        accepted=True,
        extra={"final_production_readiness_claimed": True},
    )

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="final_claimed_dep",
            object_store_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "final_claimed_dep" / "ops" / "dependency_closure.json")
    object_store = next(item for item in dependency["dependencies"] if item["dependency"] == "object_store")
    assert object_store["status"] == "blocked"
    assert object_store["summary_final_production_readiness_claimed"] is True
    assert object_store["final_production_readiness_claimed"] is False
    assert object_store["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert summary["status"] == "release_blocked"


def test_validate_ops_dependency_statuses_record_skipped_blocked_and_not_executed(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_statuses",
            dependency_statuses="slurm=skipped,object_store=skipped,met=blocked,e2e=not_executed,scale=blocked",
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "dep_statuses" / "ops" / "dependency_closure.json")
    statuses = {item["dependency"]: item["status"] for item in dependency["dependencies"]}
    assert statuses == {
        "slurm": "skipped",
        "object_store": "skipped",
        "met": "blocked",
        "e2e": "not_executed",
        "scale": "blocked",
    }
    assert dependency["deterministic_fixture"] is True
    assert summary["status"] == "release_blocked"


def test_validate_ops_rejects_explicit_accepted_dependency_status(tmp_path: Path) -> None:
    with pytest.raises(ProductionOpsValidationError) as exc_info:
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="accepted_status",
            dependency_statuses="slurm=accepted",
        )

    assert exc_info.value.error_code == "PRODUCTION_OPS_DEPENDENCY_STATUS_INVALID"


def test_validate_ops_hardens_dependency_summary_paths_and_sizes(tmp_path: Path) -> None:
    valid_root = tmp_path / "valid"
    valid_root.mkdir()
    _write_dependency_summary(
        valid_root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
    )
    symlink_root = tmp_path / "symlink-root"
    symlink_root.symlink_to(valid_root, target_is_directory=True)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_symlink_root",
            slurm_evidence_root=symlink_root,
        )
    )
    dependency = _read_json(tmp_path / "artifacts" / "dep_symlink_root" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"

    file_link_root = tmp_path / "file-link-root"
    file_link_root.mkdir()
    outside = tmp_path / "outside-summary.json"
    _write_dependency_summary(outside, "met", 149, "nhms.production_closure.met.v1", "ready")
    (file_link_root / "summary.json").symlink_to(outside)
    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_symlink_file",
            met_evidence_root=file_link_root,
        )
    )
    met = next(
        item
        for item in _read_json(tmp_path / "artifacts" / "dep_symlink_file" / "ops" / "dependency_closure.json")[
            "dependencies"
        ]
        if item["dependency"] == "met"
    )
    assert met["status"] == "blocked"
    assert met["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"

    oversized_root = tmp_path / "oversized-root"
    oversized_root.mkdir()
    (oversized_root / "summary.json").write_text(
        json.dumps(
            {
                "schema": "nhms.production_closure.scale.v1",
                "issue": 151,
                "run_id": "scale-run",
                "status": "ready",
                "payload": "x" * MAX_EVIDENCE_PAYLOAD_BYTES,
            }
        ),
        encoding="utf-8",
    )
    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_oversized",
            scale_evidence_root=oversized_root,
        )
    )
    scale = next(
        item
        for item in _read_json(tmp_path / "artifacts" / "dep_oversized" / "ops" / "dependency_closure.json")[
            "dependencies"
        ]
        if item["dependency"] == "scale"
    )
    assert scale["status"] == "blocked"
    assert scale["error_code"] == "PRODUCTION_OPS_DEPENDENCY_SUMMARY_TOO_LARGE"


def test_validate_ops_blocks_dependency_summary_swap_to_symlink_before_fd_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    symlink_target = tmp_path / "symlink-target-summary.json"
    _write_dependency_summary(
        symlink_target,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    target_bytes = symlink_target.read_bytes()
    swapped = False
    original_open = os.open

    def swap_summary_before_open(path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if dir_fd is not None and path == summary_path.name and not swapped:
            swapped = True
            summary_path.unlink()
            summary_path.symlink_to(symlink_target)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_validation_module.os, "open", swap_summary_before_open)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="summary_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "summary_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert hashlib.sha256(target_bytes).hexdigest() not in json.dumps(slurm)


def test_validate_ops_blocks_dependency_summary_swap_to_same_root_symlink_before_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    sibling = root / "sibling-summary.json"
    _write_dependency_summary(
        sibling,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    original_stat = ops_validation_module.os.stat
    swapped = False

    def swap_summary_before_no_follow_stat(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == summary_path and kwargs.get("follow_symlinks") is False and not swapped:
            swapped = True
            summary_path.unlink()
            summary_path.symlink_to(sibling)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(ops_validation_module.os, "stat", swap_summary_before_no_follow_stat)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="summary_same_root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "summary_same_root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert "sibling-summary" not in json.dumps(slurm)


def test_validate_ops_opens_dependency_summary_and_receipt_by_bound_parent_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    receipt_path = root / "accepted_dependency_evidence.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    full_path_open_attempts: list[Path] = []
    basename_opens: list[str] = []
    original_open = os.open

    def guarded_open(path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if dir_fd is None and Path(path) in {summary_path.resolve(), receipt_path.resolve()}:
            full_path_open_attempts.append(Path(path))
            raise AssertionError("dependency evidence file must be opened relative to the bound parent fd")
        if dir_fd is not None and path in {summary_path.name, receipt_path.name}:
            basename_opens.append(str(path))
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_validation_module.os, "open", guarded_open)

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="bound_parent_fd",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "bound_parent_fd" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert slurm["status"] == "accepted"
    assert full_path_open_attempts == []
    assert basename_opens == ["summary.json", "accepted_dependency_evidence.json"]


def test_validate_ops_blocks_dependency_root_swap_to_symlink_before_summary_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    outside = tmp_path / "outside-slurm"
    outside.mkdir()
    _write_dependency_summary(
        outside / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    swapped = False
    original_verify = ops_validation_module._verify_bound_directory_identity

    def swap_root_before_verify(path, expected, path_unsafe_code):
        nonlocal swapped
        if path == root.resolve() and not swapped:
            swapped = True
            summary_path.unlink()
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
        return original_verify(path, expected, path_unsafe_code)

    monkeypatch.setattr(ops_validation_module, "_verify_bound_directory_identity", swap_root_before_verify)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm


def test_validate_ops_blocks_dependency_parent_swap_to_symlink_before_summary_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "deps"
    summary_parent = root / "slurm"
    summary_parent.mkdir(parents=True)
    summary_path = summary_parent / "summary.json"
    _write_dependency_summary(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    _write_dependency_summary(
        outside / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    swapped = False
    original_verify = ops_validation_module._verify_bound_directory_identity

    def swap_parent_before_verify(path, expected, path_unsafe_code):
        nonlocal swapped
        if path == summary_parent.resolve() and not swapped:
            swapped = True
            summary_path.unlink()
            summary_parent.rmdir()
            summary_parent.symlink_to(outside, target_is_directory=True)
        return original_verify(path, expected, path_unsafe_code)

    monkeypatch.setattr(ops_validation_module, "_verify_bound_directory_identity", swap_parent_before_verify)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="parent_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "parent_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm


def test_validate_ops_blocks_dependency_receipt_swap_to_symlink_before_fd_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    symlink_target = tmp_path / "symlink-target-accepted_dependency_evidence.json"
    symlink_target.write_bytes(receipt_path.read_bytes())
    target_bytes = symlink_target.read_bytes()
    swapped = False
    original_open = os.open

    def swap_receipt_before_open(path: Path | str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if dir_fd is not None and path == receipt_path.name and not swapped:
            swapped = True
            receipt_path.unlink()
            receipt_path.symlink_to(symlink_target)
        if dir_fd is None:
            return original_open(path, flags, mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(ops_validation_module.os, "open", swap_receipt_before_open)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="receipt_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "receipt_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert hashlib.sha256(target_bytes).hexdigest() not in json.dumps(slurm)


def test_validate_ops_blocks_dependency_receipt_swap_to_same_root_symlink_before_bind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    sibling_receipt = root / "sibling-accepted_dependency_evidence.json"
    sibling_receipt.write_bytes(receipt_path.read_bytes())
    original_stat = ops_validation_module.os.stat
    swapped = False

    def swap_receipt_before_no_follow_stat(path, *args, **kwargs):
        nonlocal swapped
        if Path(path) == receipt_path and kwargs.get("follow_symlinks") is False and not swapped:
            swapped = True
            receipt_path.unlink()
            receipt_path.symlink_to(sibling_receipt)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(ops_validation_module.os, "stat", swap_receipt_before_no_follow_stat)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="receipt_same_root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "receipt_same_root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_SYMLINK"
    assert "accepted_dependency_evidence" not in slurm
    assert "sibling-accepted" not in json.dumps(slurm)


def test_validate_ops_blocks_dependency_root_swap_to_symlink_before_receipt_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    outside = tmp_path / "outside-slurm"
    outside.mkdir()
    _write_dependency_summary(outside / "summary.json", "slurm", 147, "nhms.production_closure.slurm.v1", "submitted")
    _write_dependency_acceptance_receipt(outside / "summary.json", "slurm", 147, "nhms.production_closure.slurm.v1")
    swapped = False
    verify_count = 0
    original_verify = ops_validation_module._verify_bound_directory_identity

    def swap_root_before_receipt_verify(path, expected, path_unsafe_code):
        nonlocal swapped, verify_count
        if path == root.resolve():
            verify_count += 1
            if verify_count > 1 and not swapped:
                swapped = True
                for child in root.iterdir():
                    child.unlink()
                root.rmdir()
                root.symlink_to(outside, target_is_directory=True)
        return original_verify(path, expected, path_unsafe_code)

    monkeypatch.setattr(ops_validation_module, "_verify_bound_directory_identity", swap_root_before_receipt_verify)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="receipt_root_swap",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "receipt_root_swap" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm


def test_validate_ops_blocks_dependency_root_swap_to_symlink_after_summary_read_before_receipt_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "slurm"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=False,
    )
    outside = tmp_path / "outside-slurm"
    outside.mkdir()
    outside_summary_path = outside / "summary.json"
    _write_dependency_summary(
        outside_summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    marker_receipt_digest = hashlib.sha256((outside / "accepted_dependency_evidence.json").read_bytes()).hexdigest()
    swapped = False
    original_read_summary = ops_validation_module._read_dependency_summary_json

    def swap_root_after_summary_read(summary_evidence):
        nonlocal swapped
        summary, digest = original_read_summary(summary_evidence)
        if not swapped:
            swapped = True
            summary_path.unlink()
            root.rmdir()
            root.symlink_to(outside, target_is_directory=True)
        return summary, digest

    monkeypatch.setattr(ops_validation_module, "_read_dependency_summary_json", swap_root_after_summary_read)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="root_swap_after_summary",
            slurm_evidence_root=root,
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "root_swap_after_summary" / "ops" / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert swapped is True
    assert summary["status"] == "release_blocked"
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_EVIDENCE_PATH_UNSAFE"
    assert "accepted_dependency_evidence" not in slurm
    assert marker_receipt_digest not in json.dumps(slurm)


def test_validate_ops_blocks_invalid_utf8_dependency_summary_and_writes_lane(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid-summary"
    invalid_root.mkdir()
    (invalid_root / "summary.json").write_bytes(b'{"schema": "\xff"}')

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="invalid_utf8_summary",
            slurm_evidence_root=invalid_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "invalid_utf8_summary" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_SUMMARY_INVALID"


def test_validate_ops_blocks_too_deep_dependency_summary_and_writes_lane(tmp_path: Path) -> None:
    root = tmp_path / "deep-summary"
    root.mkdir()
    summary_path = root / "summary.json"
    nested: object = "leaf"
    for _ in range(150):
        nested = [nested]
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
        extra={"bounded_nested_payload": nested},
    )
    _write_dependency_acceptance_receipt(summary_path, "slurm", 147, "nhms.production_closure.slurm.v1")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="too_deep_summary",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "too_deep_summary" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_SUMMARY_INVALID"
    assert "nesting limit" in slurm["reason"]


def test_validate_ops_blocks_invalid_utf8_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    invalid_root = tmp_path / "invalid-receipt"
    invalid_root.mkdir()
    summary_path = invalid_root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    (invalid_root / "accepted_dependency_evidence.json").write_bytes(b'{"schema": "\xff"}')

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="invalid_utf8_receipt",
            slurm_evidence_root=invalid_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "invalid_utf8_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"


def test_validate_ops_blocks_malformed_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    invalid_root = tmp_path / "malformed-receipt"
    invalid_root.mkdir()
    summary_path = invalid_root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    (invalid_root / "accepted_dependency_evidence.json").write_text('{"schema": ', encoding="utf-8")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="malformed_receipt",
            slurm_evidence_root=invalid_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "malformed_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"


def test_validate_ops_blocks_too_deep_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    root = tmp_path / "deep-receipt"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    receipt = _read_json(receipt_path)
    nested: object = "leaf"
    for _ in range(150):
        nested = [nested]
    receipt["bounded_nested_payload"] = nested
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="too_deep_receipt",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "too_deep_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"
    assert "nesting limit" in slurm["reason"]


def test_validate_ops_blocks_too_wide_dependency_receipt_and_writes_lane(tmp_path: Path) -> None:
    root = tmp_path / "wide-receipt"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )
    receipt_path = root / "accepted_dependency_evidence.json"
    receipt = _read_json(receipt_path)
    receipt["wide_nodes"] = [0] * 10_050
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="too_wide_receipt",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "too_wide_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"
    assert "complexity limit" in slurm["reason"]


def test_validate_ops_blocks_dependency_receipt_recursion_error_and_writes_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "recursive-receipt"
    root.mkdir()
    summary_path = root / "summary.json"
    _write_dependency_summary(
        summary_path,
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )

    def raise_recursion_error(receipt_path: object) -> object:
        del receipt_path
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ops_validation_module, "_read_dependency_receipt_json", raise_recursion_error)

    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="recursive_receipt",
            slurm_evidence_root=root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "recursive_receipt" / "ops"
    dependency = _read_json(lane_dir / "dependency_closure.json")
    slurm = next(item for item in dependency["dependencies"] if item["dependency"] == "slurm")
    assert summary["status"] == "release_blocked"
    assert summary["dependency_status"] == "release_blocked"
    assert (lane_dir / "summary.json").is_file()
    assert slurm["status"] == "blocked"
    assert slurm["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_INVALID"


def test_validate_ops_resolves_dependency_summary_shapes_in_rollback_references(tmp_path: Path) -> None:
    run_root = tmp_path / "dependencies"
    slurm_root = run_root / "slurm"
    object_store_root = run_root / "object-store"
    slurm_root.mkdir(parents=True)
    object_store_root.mkdir()
    _write_dependency_summary(
        slurm_root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
    )
    _write_dependency_summary(
        object_store_root / "summary.json",
        "object_store",
        148,
        "nhms.production_closure.object_store.v1",
        "ready",
    )

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_shapes",
            slurm_evidence_root=run_root,
            object_store_evidence_root=run_root,
        )
    )

    rollback = _read_json(tmp_path / "artifacts" / "dep_shapes" / "ops" / "rollback_drills.json")
    first_drill_refs = rollback["drills"][0]["dependency_artifact_references"]
    assert {"dependency": "slurm", "drill": "bad_model_activation", "summary": str(slurm_root / "summary.json")} in (
        first_drill_refs
    )
    assert {
        "dependency": "object_store",
        "drill": "bad_model_activation",
        "summary": str(object_store_root / "summary.json"),
    } in first_drill_refs


def test_validate_ops_live_drill_scope_remains_release_blocked_without_receipts(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="live_scope",
            rollback_scope="live_drill",
        )
    )

    rollback = _read_json(tmp_path / "artifacts" / "live_scope" / "ops" / "rollback_drills.json")
    assert summary["status"] == "release_blocked"
    assert summary["live_rollback_executed"] is False
    assert rollback["status"] == "release_blocked"
    assert rollback["requested_scope"] == "live_drill"
    assert rollback["live_rollback_executed"] is False
    assert {drill["execution_mode"] for drill in rollback["drills"]} == {"simulated_drill"}
    assert {drill["live_rollback_executed"] for drill in rollback["drills"]} == {False}


def test_validate_ops_live_ready_config_knobs_do_not_claim_live_execution(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="live_knobs",
            auth_mode="backend_route_executed",
            alert_target="https://alerts.example/ops",
        )
    )

    lane_dir = tmp_path / "artifacts" / "live_knobs" / "ops"
    auth = _read_json(lane_dir / "auth_rbac.json")
    alerts = _read_json(lane_dir / "monitoring_alerts.json")
    assert summary["live_backend_auth_executed"] is False
    assert summary["live_alert_sink_delivered"] is False
    assert auth["model_activation_boundary"]["backend_enforcement_available"] is True
    assert auth["model_activation_boundary"]["requested_auth_mode"] == "backend_route_executed"
    assert alerts["status"] == "release_blocked"
    assert {alert["execution_mode"] for alert in alerts["alerts"]} == {"not_executed"}
    assert {alert["sink"] for alert in alerts["alerts"]} == {"https://alerts.example/[redacted-alert-path]"}


def test_validate_ops_env_supplied_config_values_are_not_marked_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for service, settings in {
        "api": ("DATABASE_URL", "AUTH_BACKEND", "AUDIT_LOG_DESTINATION", "CORS_ALLOWED_ORIGINS"),
        "orchestrator": ("PIPELINE_DATABASE_URL", "OBJECT_STORE_PREFIX", "SLURM_GATEWAY_URL", "WORKSPACE_ROOT"),
        "slurm_gateway": ("SLURM_PARTITION", "SLURM_ACCOUNT", "SLURM_SHARED_LOG_ROOT", "SBATCH_TEMPLATE_ROOT"),
        "tile_publisher": ("TILE_OBJECT_PREFIX", "TILE_LAYER_REGISTRY", "TILE_ERROR_TOPIC"),
        "frontend": ("VITE_API_BASE_URL", "VITE_AUTH_MODE", "VITE_MAP_STYLE_URL"),
        "database": ("DATABASE_URL", "POSTGIS_ENABLED", "TIMESCALE_ENABLED", "MIGRATION_LOCK"),
        "object_store": ("OBJECT_STORE_ROOT", "OBJECT_STORE_PREFIX", "OBJECT_STORE_CREDENTIAL_SOURCE"),
        "source_adapters": ("GFS_CONFIG", "IFS_CONFIG", "ERA5_CONFIG", "CLDAS_RESTRICTED_REASON"),
        "workspace_roots": ("RUN_WORKSPACE_ROOT", "SHARED_LOG_ROOT", "ARTIFACT_RETENTION_POLICY"),
    }.items():
        for setting in settings:
            monkeypatch.setenv(f"NHMS_PRODUCTION_OPS_{service.upper()}_{setting}", _safe_config_value(setting))

    validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="env_config"))

    config = _read_json(tmp_path / "artifacts" / "env_config" / "ops" / "config_validation.json")
    assert not [
        blocker for blocker in config["blockers"] if blocker["error_code"] == "PRODUCTION_OPS_CONFIG_MISSING_SETTING"
    ]
    for service in config["services"]:
        assert all(item["source"] == "environment" for item in service["setting_source_metadata"])
        assert all(item["missing_required"] is False for item in service["setting_source_metadata"])


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("NHMS_PRODUCTION_OPS_ORCHESTRATOR_WORKSPACE_ROOT", "../workspace"),
        ("NHMS_PRODUCTION_OPS_SLURM_GATEWAY_SLURM_SHARED_LOG_ROOT", "/scratch/../logs"),
        ("NHMS_PRODUCTION_OPS_SLURM_GATEWAY_SBATCH_TEMPLATE_ROOT", "templates\\prod"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_ROOT", "s3://bucket/%2E%2E/root"),
        ("NHMS_PRODUCTION_OPS_WORKSPACE_ROOTS_RUN_WORKSPACE_ROOT", "runs/%2E"),
        ("NHMS_PRODUCTION_OPS_WORKSPACE_ROOTS_SHARED_LOG_ROOT", "logs%5Cprod"),
    ],
)
def test_validate_ops_rejects_unsafe_config_root_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_name, value)

    with pytest.raises(ProductionOpsValidationError) as exc_info:
        validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="bad_config_root"))

    assert exc_info.value.error_code == "PRODUCTION_OPS_CONFIG_VALUE_UNSAFE"


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_ROOT", "s3://../prod"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_ROOT", "file://../workspace"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_ROOT", "s3://%2E%2E/prod"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_ROOT", "s3://bucket%2Fprod/root"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_PREFIX", "s3://bucket/%2E%2E/root"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_PREFIX", "s3://bucket/path%2Ftoken"),
        ("NHMS_PRODUCTION_OPS_OBJECT_STORE_OBJECT_STORE_PREFIX", "s3://bucket/path/access_key=secret"),
        ("NHMS_PRODUCTION_OPS_TILE_PUBLISHER_TILE_OBJECT_PREFIX", "s3://bucket/%2E%2E/tiles"),
        ("NHMS_PRODUCTION_OPS_TILE_PUBLISHER_TILE_OBJECT_PREFIX", "s3://bucket/tiles%5Cprod"),
        ("NHMS_PRODUCTION_OPS_TILE_PUBLISHER_TILE_OBJECT_PREFIX", "s3://bucket/tiles/token=secret"),
        ("NHMS_PRODUCTION_OPS_ORCHESTRATOR_OBJECT_STORE_PREFIX", "s3://bucket/%2E%2E/orchestrator"),
        ("NHMS_PRODUCTION_OPS_ORCHESTRATOR_OBJECT_STORE_PREFIX", "s3://bucket/orchestrator%2Fprod"),
        ("NHMS_PRODUCTION_OPS_ORCHESTRATOR_OBJECT_STORE_PREFIX", "s3://bucket/orchestrator/api_key=secret"),
    ],
)
def test_validate_ops_rejects_unsafe_config_url_authorities_and_prefixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_name: str,
    value: str,
) -> None:
    monkeypatch.setenv(env_name, value)

    with pytest.raises(ProductionOpsValidationError) as exc_info:
        validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="bad_config_prefix"))

    assert exc_info.value.error_code == "PRODUCTION_OPS_CONFIG_VALUE_UNSAFE"


def test_validate_ops_run_id_idempotency_path_safety_payload_limit_and_secret_redaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = ["--evidence-root", str(tmp_path / "artifacts"), "--run-id", "rerun"]
    assert _argparse_main(args) == 0
    assert _argparse_main(args) == 1
    assert "PRODUCTION_OPS_EVIDENCE_EXISTS" in capsys.readouterr().err
    assert _argparse_main([*args, "--force"]) == 0

    with pytest.raises(ProductionOpsValidationError) as exc_info:
        ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="../escape")
    assert exc_info.value.error_code == "PRODUCTION_OPS_RUN_ID_UNSAFE"

    symlink_root = tmp_path / "symlink-root"
    target_root = tmp_path / "target-root"
    target_root.mkdir()
    symlink_root.symlink_to(target_root, target_is_directory=True)
    with pytest.raises(ProductionOpsValidationError) as symlink_exc:
        ProductionOpsConfig.from_env(evidence_root=symlink_root, run_id="safe")
    assert symlink_exc.value.error_code == "PRODUCTION_OPS_EVIDENCE_SYMLINK"

    for suffix in ("new-root", "missing/deep"):
        with pytest.raises(ProductionOpsValidationError) as nested_symlink_exc:
            ProductionOpsConfig.from_env(evidence_root=symlink_root / suffix, run_id="safe")
        assert nested_symlink_exc.value.error_code == "PRODUCTION_OPS_EVIDENCE_SYMLINK"
        assert not (target_root / suffix).exists()

    config = ProductionOpsConfig.from_env(evidence_root=tmp_path / "payload", run_id="payload")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True, max_payload_bytes=64)
    writer.prepare()
    with pytest.raises(ProductionOpsValidationError) as payload_exc:
        writer.write_json(config.lane_dir / "too_large.json", {"payload": "x" * 256})
    assert payload_exc.value.error_code == "PRODUCTION_OPS_EVIDENCE_PAYLOAD_TOO_LARGE"

    swap_config = ProductionOpsConfig.from_env(evidence_root=tmp_path / "swap", run_id="swap")
    swap_writer = EvidenceWriter(swap_config.evidence_root, swap_config.lane_dir, force=True)
    swap_writer.prepare()
    external = tmp_path / "external-swap"
    external.mkdir()
    original_verify = safe_fs._verify_fd_matches_path
    swapped = False

    def swap_lane_parent(fd: int, path: Path) -> None:
        nonlocal swapped
        if path == swap_config.lane_dir and not swapped:
            swapped = True
            swap_config.lane_dir.rmdir()
            swap_config.lane_dir.symlink_to(external, target_is_directory=True)
        original_verify(fd, path)

    monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", swap_lane_parent)
    with pytest.raises(ProductionOpsValidationError) as swap_exc:
        swap_writer.write_json(swap_config.lane_dir / "summary.json", {"status": "ready"})
    assert swap_exc.value.error_code == "PRODUCTION_OPS_EVIDENCE_PATH_UNSAFE"
    assert not (external / "summary.json").exists()

    monkeypatch.setenv("AUTH_TOKEN", "supersecret")
    exit_code = slurm_validation._argparse_main(
        [
            "validate-ops",
            "--evidence-root",
            str(tmp_path / "redacted"),
            "--run-id",
            "bad_target",
            "--alert-target",
            "https://alerts.example/path/password=supersecret",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "supersecret" not in captured.out
    assert "supersecret" not in captured.err
    assert "PRODUCTION_OPS_ALERT_TARGET_UNSAFE" in captured.err

    assert (
        slurm_validation._argparse_main(
            [
                "validate-ops",
                "--evidence-root",
                str(tmp_path / "redacted"),
                "--run-id",
                "redacted",
            ]
        )
        == 0
    )
    assert "supersecret" not in capsys.readouterr().out
    evidence_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "redacted" / "redacted" / "ops").glob("*.json")
    )
    assert "supersecret" not in evidence_text
    assert "deterministic-secret-for-redaction-test" not in evidence_text
    assert "[redacted]" in evidence_text


def test_validate_ops_existing_lane_regular_file_raises_stable_error(tmp_path: Path) -> None:
    lane_path = tmp_path / "artifacts" / "file_lane" / "ops"
    lane_path.parent.mkdir(parents=True)
    lane_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ProductionOpsValidationError) as exc_info:
        validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="file_lane"))

    assert exc_info.value.error_code == "PRODUCTION_OPS_EVIDENCE_PATH_UNSAFE"


def test_validate_ops_sanitizes_path_embedded_alert_target_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_target = "https://hooks.example/services/T00000000/B00000000/raw-path-webhook-token"
    monkeypatch.setenv("NHMS_PRODUCTION_OPS_ALERT_TARGET", raw_target)

    assert _argparse_main(["--evidence-root", str(tmp_path / "artifacts"), "--run-id", "alert_path_token"]) == 0
    captured = capsys.readouterr()
    assert "T00000000" not in captured.out
    assert "B00000000" not in captured.out
    assert "raw-path-webhook-token" not in captured.out

    lane_dir = tmp_path / "artifacts" / "alert_path_token" / "ops"
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.glob("*.json"))
    assert "T00000000" not in evidence_text
    assert "B00000000" not in evidence_text
    assert "raw-path-webhook-token" not in evidence_text
    assert "https://hooks.example/[redacted-alert-path]" in evidence_text


def test_validate_ops_sanitizes_path_embedded_dry_run_alert_target_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_target = "dry-run://sink/raw-token/path"
    monkeypatch.setenv("NHMS_PRODUCTION_OPS_ALERT_TARGET", raw_target)

    assert _argparse_main(["--evidence-root", str(tmp_path / "artifacts"), "--run-id", "dry_run_path_token"]) == 0
    captured = capsys.readouterr()
    assert "raw-token" not in captured.out
    assert "/path" not in captured.out

    lane_dir = tmp_path / "artifacts" / "dry_run_path_token" / "ops"
    evidence_text = "\n".join(path.read_text(encoding="utf-8") for path in lane_dir.glob("*.json"))
    assert "raw-token" not in evidence_text
    assert "dry-run://sink/raw-token/path" not in evidence_text
    assert "dry-run://sink/[redacted-alert-path]" in evidence_text


@pytest.mark.parametrize(
    "alert_target",
    [
        "https://alerts.example/path%2Ftoken=secret",
        "https://alerts.example/path%252Fpassword=secret",
        "https://alerts.example/path%2F..",
        "https://alerts.example/path%3Fsignature=secret",
    ],
)
def test_validate_ops_rejects_encoded_alert_target_secrets(tmp_path: Path, alert_target: str) -> None:
    with pytest.raises(ProductionOpsValidationError) as exc_info:
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="encoded_secret",
            alert_target=alert_target,
        )

    assert exc_info.value.error_code == "PRODUCTION_OPS_ALERT_TARGET_UNSAFE"


def test_validate_ops_rejects_over_encoded_alert_target_secret(tmp_path: Path) -> None:
    encoded_secret_segment = _percent_encode_rounds("/token=secret", MAX_PERCENT_DECODE_ROUNDS + 1)

    with pytest.raises(ProductionOpsValidationError) as exc_info:
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="over_encoded_secret",
            alert_target=f"https://alerts.example/path{encoded_secret_segment}",
        )

    assert exc_info.value.error_code == "PRODUCTION_OPS_ALERT_TARGET_UNSAFE"


def test_validate_ops_click_and_argparse_dispatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    click_exit = slurm_validation._click_main(
        [
            "validate-ops",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "click",
        ]
    )
    assert click_exit == 0
    click_summary = json.loads(capsys.readouterr().out)
    assert click_summary["schema"] == "nhms.production_closure.ops.v1"
    assert click_summary["status"] == "release_blocked"

    argparse_exit = _argparse_main(
        [
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "argparse",
        ]
    )
    assert argparse_exit == 0
    argparse_summary = json.loads(capsys.readouterr().out)
    assert argparse_summary["status"] == "release_blocked"

    combined_argparse_exit = slurm_validation._argparse_main(
        [
            "validate-ops",
            "--evidence-root",
            str(tmp_path / "artifacts"),
            "--run-id",
            "combined",
        ]
    )
    assert combined_argparse_exit == 0
    combined_summary = json.loads(capsys.readouterr().out)
    assert combined_summary["schema"] == "nhms.production_closure.ops.v1"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_dependency_summary(
    path: Path,
    name: str,
    issue: int,
    schema: str,
    status: str,
    *,
    accepted: bool = False,
    extra: dict | None = None,
) -> None:
    payload = {
        "schema": schema,
        "issue": issue,
        "run_id": f"{name}-run",
        "status": status,
        "evidence_dir": str(path.parent),
    }
    if accepted:
        live_fields_by_dependency = {
            "slurm": {"live_slurm_executed": True, "live_slurm_status": "executed"},
            "object_store": {
                "live_registry_import": True,
                "live_api": True,
                "live_api_status": "executed",
            },
            "met": {"live_met_executed": True, "live_source_count": 1},
            "e2e": {
                "live_db_executed": True,
                "live_api_executed": True,
                "live_slurm_executed": True,
                "live_frontend_executed": True,
            },
            "scale": {
                "live_db_executed": True,
                "live_api_executed": True,
                "live_frontend_executed": True,
            },
        }
        payload.update(
            {
                "execution_mode": "accepted_live_evidence",
                "deterministic_fixture": False,
                "final_production_readiness_claimed": False,
                **live_fields_by_dependency[name],
            }
        )
    if extra:
        payload.update(extra)
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    if accepted:
        _write_dependency_acceptance_receipt(path, name, issue, schema)


def _write_dependency_acceptance_receipt(path: Path, name: str, issue: int, schema: str) -> None:
    summary = _read_json(path)
    receipt = {
        "schema": "nhms.production_closure.ops.accepted_dependency_evidence.v1",
        "accepted": True,
        "dependency": name,
        "issue": issue,
        "summary_schema": schema,
        "summary_run_id": summary["run_id"],
        "summary_path": str(path.resolve()),
        "summary_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "receipt_id": f"ops-acceptance-{name}-7f43c0e2b47041f0a6d3107b0f76c234",
        "accepted_at": "2026-05-17T00:00:00Z",
        "execution_mode": "accepted_live_evidence",
        "deterministic_fixture": False,
        "final_production_readiness_claimed": False,
    }
    (path.parent / "accepted_dependency_evidence.json").write_text(json.dumps(receipt), encoding="utf-8")


def _safe_config_value(setting: str) -> str:
    if "URL" in setting:
        return "https://prod.example/internal"
    if "ROOT" in setting:
        return "/srv/nhms/prod"
    if "PREFIX" in setting:
        return "s3://nhms-prod/releases"
    if setting.endswith("ENABLED"):
        return "true"
    if setting.endswith("REASON"):
        return "restricted-source-approved-by-ops"
    return f"prod_{setting.lower()}"


def _percent_encode_rounds(value: str, rounds: int) -> str:
    encoded = value
    for _ in range(rounds):
        encoded = quote(encoded, safe="")
    return encoded
