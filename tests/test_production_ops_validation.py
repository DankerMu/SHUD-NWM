from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from services.production_closure import slurm_validation
from services.production_closure.ops_validation import (
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
        (root / "summary.json").write_text(
            json.dumps(
                {
                    "schema": schema,
                    "issue": issue,
                    "run_id": f"{name}-run",
                    "status": status,
                    "evidence_dir": str(root),
                    "final_production_readiness_claimed": False,
                }
            ),
            encoding="utf-8",
        )
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


def test_validate_ops_dependency_statuses_record_skipped_blocked_and_not_executed(tmp_path: Path) -> None:
    summary = validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dep_statuses",
            dependency_statuses="slurm=accepted,object_store=skipped,met=blocked,e2e=not_executed,scale=accepted",
        )
    )

    dependency = _read_json(tmp_path / "artifacts" / "dep_statuses" / "ops" / "dependency_closure.json")
    statuses = {item["dependency"]: item["status"] for item in dependency["dependencies"]}
    assert statuses == {
        "slurm": "accepted",
        "object_store": "skipped",
        "met": "blocked",
        "e2e": "not_executed",
        "scale": "accepted",
    }
    assert dependency["deterministic_fixture"] is True
    assert summary["status"] == "release_blocked"


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


def _percent_encode_rounds(value: str, rounds: int) -> str:
    encoded = value
    for _ in range(rounds):
        encoded = quote(encoded, safe="")
    return encoded
