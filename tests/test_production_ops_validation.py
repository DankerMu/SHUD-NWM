from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from packages.common import safe_fs
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
    assert summary["evidence_dir"] == "m10_152/ops"
    assert summary["live_backend_auth_executed"] is False
    assert summary["auth_readiness_execution_mode"] == "release_blocked"
    assert summary["live_alert_sink_delivered"] is False
    assert summary["live_rollback_executed"] is False
    assert summary["model_lifecycle_drill_status"] == "ready"
    assert summary["model_lifecycle_live_object_store_mutation"] is False
    assert summary["dependency_status"] == "release_blocked"
    assert summary["files"] == [
        "preflight.json",
        "config_validation.json",
        "auth_rbac.json",
        "auth_release_blockers.json",
        "audit_redaction.json",
        "model_lifecycle_drills.json",
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


def test_validate_ops_summary_redacts_absolute_evidence_path(tmp_path: Path) -> None:
    evidence_root = tmp_path / "absolute-evidence-root"
    validate_ops(ProductionOpsConfig.from_env(evidence_root=evidence_root, run_id="redacted_path"))

    lane_dir = evidence_root / "redacted_path" / "ops"
    artifacts = {
        path.name: _read_json(path)
        for path in lane_dir.glob("*.json")
    }
    summary = artifacts["summary.json"]
    rendered = json.dumps(artifacts)

    assert summary["evidence_dir"] == "redacted_path/ops"
    assert str(tmp_path) not in rendered
    assert str(evidence_root) not in rendered
    assert str(lane_dir) not in rendered
    assert str(Path.cwd()) not in rendered


def test_validate_ops_dependency_paths_are_redacted_from_json_artifacts(tmp_path: Path) -> None:
    dependency_root = tmp_path / "slurm-dependency-root"
    dependency_root.mkdir()
    _write_dependency_summary(
        dependency_root / "summary.json",
        "slurm",
        147,
        "nhms.production_closure.slurm.v1",
        "submitted",
        accepted=True,
    )

    validate_ops(
        ProductionOpsConfig.from_env(
            evidence_root=tmp_path / "artifacts",
            run_id="dependency_paths",
            slurm_evidence_root=dependency_root,
        )
    )

    lane_dir = tmp_path / "artifacts" / "dependency_paths" / "ops"
    rendered = json.dumps({path.name: _read_json(path) for path in lane_dir.glob("*.json")})
    assert str(tmp_path) not in rendered
    assert str(dependency_root) not in rendered
    assert str(Path.cwd()) not in rendered


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


def test_validate_ops_model_lifecycle_drill_is_deterministic_without_live_object_store(tmp_path: Path) -> None:
    validate_ops(ProductionOpsConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="model_lifecycle"))
    lifecycle = _read_json(tmp_path / "artifacts" / "model_lifecycle" / "ops" / "model_lifecycle_drills.json")

    assert lifecycle["schema"] == "nhms.production_closure.ops.model_lifecycle_drills.v1"
    assert lifecycle["status"] == "ready"
    assert lifecycle["execution_mode"] == "deterministic_fixture"
    assert lifecycle["live_object_store_mutation"] is False
    assert lifecycle["live_external_material_required"] is False
    assert {item["drill"] for item in lifecycle["drills"]} == {
        "bad_activation_preflight",
        "rollback_to_previous_active",
        "blocked_deactivation_missing_active",
        "idempotent_repeat_activation",
    }
    bad_activation = next(item for item in lifecycle["drills"] if item["drill"] == "bad_activation_preflight")
    assert bad_activation["status"] == "blocked"
    assert bad_activation["blockers"] == [{"code": "OBJECT_URI_PREFIX_INVALID"}]
    assert all(item["object_store_mutated"] is False for item in lifecycle["drills"])
    assert set(lifecycle["non_goals"]) == {
        "arbitrary_model_package_upload",
        "production_object_store_delete",
        "production_object_store_upload",
    }



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


def _assert_stable_auth_blocker_ids(
    auth: dict[str, Any],
    auth_release_blockers: dict[str, Any],
    summary: dict[str, Any],
) -> None:
    auth_blockers = list(auth.get("blockers", []))
    release_blockers = list(auth_release_blockers.get("blockers", []))
    summary_auth_blockers = [
        blocker
        for blocker in summary.get("release_blockers", [])
        if blocker.get("blocker_id", "").startswith("m17-auth:")
    ]

    assert auth_blockers or release_blockers or summary_auth_blockers
    for blocker in auth_blockers + release_blockers + summary_auth_blockers:
        blocker_id = blocker.get("blocker_id")
        assert isinstance(blocker_id, str)
        assert blocker_id.startswith("m17-auth:")
        assert blocker_id.strip() == blocker_id
        assert len(blocker_id) > len("m17-auth:")


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
