from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pytest

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
    assert set(preflight["required_roles"]) >= {"operator", "model_admin", "source_admin", "tile_admin"}
    assert preflight["alert_target"] == "dry-run://ops-validation"
    assert preflight["deployment_config_source"] == "generated_deterministic_templates"
    assert preflight["rollback_drill_scope"] == "simulated_drills"
    assert preflight["evidence_dir"] == str(lane_dir)
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
        "model_activation",
        "rerun",
        "cancel",
        "qc_override",
        "source_config_change",
        "tile_republish",
    }
    assert {item["action"] for item in auth["action_decisions"]} == expected_actions
    assert {item["decision"] for item in auth["action_decisions"]} == {
        "allowed",
        "denied",
        "release_blocked",
    }
    assert set(auth["execution_modes"]) == {"policy_simulated", "release_blocked"}
    assert auth["live_backend_auth_executed"] is False
    assert auth["state_mutation_assertions"] == {
        "denied_actions_mutated_state": False,
        "release_blocked_actions_mutated_state": False,
    }
    for decision in auth["action_decisions"]:
        if decision["decision"] in {"denied", "release_blocked"}:
            assert decision["previous_state"] == decision["new_state"]
            assert decision["state_mutated"] is False
            assert decision["error_code"] in {
                "PRODUCTION_OPS_RBAC_FORBIDDEN",
                "PRODUCTION_OPS_BACKEND_AUTH_RELEASE_BLOCKED",
            }

    assert blockers["status"] == "release_blocked"
    assert {item["action"] for item in blockers["blockers"]} == expected_actions
    assert all(item["residual_risk"] and item["removal_criteria"] for item in blockers["blockers"])

    assert audit["status"] == "ready"
    assert len(audit["audit_rows"]) == len(auth["action_decisions"])
    first_row = audit["audit_rows"][0]
    assert {"actor", "role", "target", "previous_state", "new_state", "decision", "reason", "lineage"} <= set(
        first_row
    )
    assert {row["decision"] for row in audit["audit_rows"]} == {"allowed", "denied", "release_blocked"}


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


def test_validate_ops_rejects_object_store_fast_summary_even_with_receipt(tmp_path: Path) -> None:
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
    assert object_store["status"] == "skipped"
    assert object_store["deterministic_fixture"] is True
    assert object_store["error_code"] == "PRODUCTION_OPS_DEPENDENCY_ACCEPTED_EVIDENCE_MISSING"
    assert "deterministic/non-live" in object_store["reason"]


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
    assert auth["model_activation_boundary"]["backend_enforcement_available"] is False
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

    config = ProductionOpsConfig.from_env(evidence_root=tmp_path / "payload", run_id="payload")
    writer = EvidenceWriter(config.evidence_root, config.lane_dir, force=True, max_payload_bytes=64)
    writer.prepare()
    with pytest.raises(ProductionOpsValidationError) as payload_exc:
        writer.write_json(config.lane_dir / "too_large.json", {"payload": "x" * 256})
    assert payload_exc.value.error_code == "PRODUCTION_OPS_EVIDENCE_PAYLOAD_TOO_LARGE"

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
        payload.update(
            {
                "execution_mode": "accepted_live_evidence",
                "deterministic_fixture": False,
                "final_production_readiness_claimed": False,
                f"live_{name}_executed": True,
            }
        )
        if name == "object_store":
            payload.update(
                {
                    "live_registry_import": True,
                    "live_api": True,
                    "live_api_status": "executed",
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
