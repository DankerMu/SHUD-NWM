from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.production_closure import slurm_validation
from services.production_closure.readiness_validation import (
    ALLOWED_STATUS_EXECUTION_MODES,
    EXECUTION_MODE_VALUES,
    ProductionReadinessConfig,
    ProductionReadinessValidationError,
    validate_readiness,
    validate_readiness_item,
)

LIVE_SCHEMA = "nhms.production_readiness.live_proof.v1"
SCHEDULER_SCHEMA = "nhms.production_scheduler.pass_evidence.v1"
PROOF_SURFACES = {
    "auth": "live_backend_auth",
    "alert": "live_alert_sink_delivery",
    "rollback": "live_rollback_execution",
    "scheduler": "live_scheduler_evidence_proof",
    "slurm": "live_slurm_dependency_proof",
    "object_store": "live_object_store_dependency_proof",
    "source": "live_source_weather_dependency_proof",
    "e2e": "live_e2e_dependency_proof",
    "mvt": "live_mvt_performance_proof",
    "target_env": "target_environment_config_proof",
}
DEPENDENCY_PROOFS = {"slurm", "object_store", "source", "e2e", "mvt"}
DEPENDENCY_CONTRACTS = {
    "slurm": (147, "nhms.production_closure.slurm.v1"),
    "object_store": (148, "nhms.production_closure.object_store.v1"),
    "source": (149, "nhms.production_closure.met.v1"),
    "e2e": (150, "nhms.production_closure.e2e.v1"),
    "mvt": (151, "nhms.production_closure.scale.v1"),
}


def _summary(root: Path, run_id: str = "m19") -> dict[str, object]:
    return json.loads((root / run_id / "readiness" / "summary.json").read_text(encoding="utf-8"))


def _items(root: Path, run_id: str = "m19") -> list[dict[str, object]]:
    payload = json.loads((root / run_id / "readiness" / "readiness_items.json").read_text(encoding="utf-8"))
    return list(payload["items"])


def _blockers(root: Path, run_id: str = "m19") -> list[dict[str, object]]:
    payload = json.loads((root / run_id / "readiness" / "release_blockers.json").read_text(encoding="utf-8"))
    return list(payload["blockers"])


def _base_item(status: str, execution_mode: str) -> dict[str, object]:
    return {
        "surface": "unit",
        "status": status,
        "execution_mode": execution_mode,
        "required_for_final": False,
        "live_proof_accepted": False,
        "artifact_refs": [],
        "residual_risk": "unit residual risk",
        "removal_criteria": "unit removal criteria",
        "exclusions": [],
    }


def _bound_proof(proof_key: str, *, run_id: str = "m19", **extra: object) -> str:
    payload: dict[str, object] = {
        "schema": LIVE_SCHEMA,
        "proof_type": (
            "dependency"
            if proof_key in DEPENDENCY_PROOFS
            else "scheduler_evidence"
            if proof_key == "scheduler"
            else proof_key
        ),
        "surface": PROOF_SURFACES[proof_key],
        "run_id": run_id,
        "target_environment": "production",
        "execution_mode": "live_proof",
        "accepted": True,
        "status": "passed",
        "artifact_refs": [f"evidence/{run_id}/{proof_key}/receipt.json"],
    }
    if proof_key in DEPENDENCY_PROOFS:
        issue, schema = DEPENDENCY_CONTRACTS[proof_key]
        payload |= {
            "dependency_surface": proof_key,
            "producer_issue": issue,
            "producer_schema": schema,
            "producer_run_id": f"{proof_key}-live-run",
            "producer_artifact_ref": f"{proof_key}:summary.json",
            "summary_checksum": "sha256:abc123",
            "provenance": {
                "dependency": proof_key,
                "producer_issue": issue,
                "producer_schema": schema,
                "producer_run_id": f"{proof_key}-live-run",
                "producer_artifact_ref": f"{proof_key}:summary.json",
                "summary_checksum": "sha256:abc123",
                "receipt_id": "sha256:abc123",
            },
        }
    elif proof_key == "alert":
        payload |= {
            "sink_metadata": {"sink": "pagerduty-prod", "channel": "release-readiness"},
            "delivery_metadata": {
                "message_id": "alert-123",
                "delivered_at": "2026-05-21T00:00:00Z",
                "result": "delivered",
            },
            "delivered": True,
        }
    elif proof_key == "rollback":
        payload |= {
            "preconditions": {"backup_verified": True, "freeze_window": "approved"},
            "command_metadata": {"command": "nhms-production rollback --dry-run=false", "drill_id": "rb-123"},
            "executed": True,
        }
    elif proof_key == "target_env":
        payload |= {
            "config_metadata": {"cluster": "shudhpc", "object_store": "prod", "database": "prod-postgis"},
            "config_receipt_id": "target-env-123",
        }
    payload |= extra
    return json.dumps(payload)


def _auth_proof(*, allowed: list[str] | None = None, denied: list[str] | None = None, **extra: object) -> str:
    payload: dict[str, object] = {
        "schema": LIVE_SCHEMA,
        "proof_type": "auth",
        "surface": "live_backend_auth",
        "run_id": "m19",
        "target_environment": "production",
        "execution_mode": "live_proof",
        "artifact_refs": ["evidence/m19/auth/receipt.json"],
        "status": "passed",
        "accepted": True,
        "provider": {
            "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
            "client_secret": "super-secret",
        },
        "role_mapping": {"operator": ["pipeline.retry_run"], "model_admin": ["models.activate"]},
        "allowed_actions": allowed or [],
        "denied_actions": denied or [],
        **extra,
    }
    return json.dumps(payload)


def _all_live_proofs() -> dict[str, str]:
    actions = _all_auth_actions()
    return {
        "auth_proof": _auth_proof(allowed=actions, denied=actions),
        "alert_proof": _bound_proof("alert"),
        "rollback_proof": _bound_proof("rollback"),
        "slurm_proof": _bound_proof("slurm"),
        "object_store_proof": _bound_proof("object_store"),
        "source_proof": _bound_proof("source"),
        "e2e_proof": _bound_proof("e2e"),
        "mvt_proof": _bound_proof("mvt"),
        "target_env_proof": _bound_proof("target_env"),
    }


def _all_auth_actions() -> list[str]:
    return [
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
    ]


def _write_dependency_summary(root: Path, proof_key: str, *, run_id: str | None = None) -> str:
    issue, schema = DEPENDENCY_CONTRACTS[proof_key]
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": schema,
        "issue": issue,
        "run_id": run_id or f"{proof_key}-live-run",
        "status": "ready",
        "execution_mode": "deterministic_fixture",
        "final_production_readiness_claimed": False,
    }
    summary_path = root / "summary.json"
    summary_path.write_text(json.dumps(payload), encoding="utf-8")
    return "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()


def _dependency_proof_bound_to_summary(proof_key: str, root: Path, *, run_id: str = "m19", **extra: object) -> str:
    checksum = _write_dependency_summary(root, proof_key)
    payload = json.loads(_bound_proof(proof_key, run_id=run_id))
    payload["summary_checksum"] = checksum
    payload["producer_checksum"] = checksum
    payload["provenance"]["summary_checksum"] = checksum
    payload["provenance"]["producer_checksum"] = checksum
    payload["provenance"]["receipt_id"] = checksum
    payload |= extra
    return json.dumps(payload)


def _dependency_proof_with_artifact_paths(
    proof_key: str,
    *,
    top_level_path: str,
    provenance_path: str,
) -> str:
    payload = json.loads(_bound_proof(proof_key))
    payload.pop("producer_artifact_ref", None)
    payload["producer_artifact_path"] = top_level_path
    payload["provenance"].pop("producer_artifact_ref", None)
    payload["provenance"]["producer_artifact_path"] = provenance_path
    return json.dumps(payload)


def _scheduler_evidence_payload(
    *,
    pass_id: str = "scheduler_20260521120000_fixed",
    status: str = "planned",
    execution_mode: str = "dry_run",
    final_claimed: bool = False,
    no_mutation_proof: dict[str, bool] | None = None,
    **extra: object,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": SCHEDULER_SCHEMA,
        "pass_id": pass_id,
        "status": status,
        "execution_mode": execution_mode,
        "started_at": "2026-05-21T12:00:00Z",
        "finished_at": "2026-05-21T12:00:01Z",
        "sources": ["gfs", "IFS"],
        "operator_filters": {
            "model_ids": ["model_a"],
            "basin_ids": [],
            "expression": "model_id in [model_a]",
            "excluded_runnable_count": 1,
        },
        "counts": {
            "candidate_count": 2,
            "blocked_candidate_count": 0,
            "skipped_candidate_count": 1,
            "selected_model_count": 1,
            "source_cycle_count": 2,
            "submitted_count": 0,
            "failed_count": 0,
            "partial_count": 0,
        },
        "candidates": [
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "source_id": "gfs",
                "cycle_time_utc": "2026-05-21T06:00:00Z",
                "model_id": "model_a",
                "run_id": "fcst_gfs_2026052106_model_a",
                "forcing_version_id": "forc_gfs_2026052106_model_a",
            }
        ],
        "skipped_candidates": [
            {
                "candidate_id": "IFS:2026-05-21T06:00:00Z:model_a:forecast_ifs_deterministic",
                "source_id": "IFS",
                "cycle_time_utc": "2026-05-21T06:00:00Z",
                "model_id": "model_a",
                "reason": "source_cycle_unavailable",
            }
        ],
        "model_run_evidence": [],
        "artifact_path": "workspace/.nhms-workspace/scheduler/evidence/scheduler_20260521120000_fixed.json",
        "execution_boundary": "planning_only",
        "no_mutation_proof": no_mutation_proof
        or {
            "adapter_download_called": False,
            "slurm_submit_called": False,
            "shud_runtime_called": False,
            "hydro_result_table_writes": False,
            "met_result_table_writes": False,
        },
        "readiness": {
            "deterministic_fixture": True,
            "live_receipts": [],
            "production_ready": final_claimed,
        },
        "final_production_readiness_claimed": final_claimed,
    }
    payload |= extra
    return payload


def _write_scheduler_evidence(path: Path, **overrides: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _scheduler_evidence_payload(**overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _scheduler_proof_bound_to_evidence(
    evidence_path: Path,
    *,
    run_id: str = "m19",
    producer_artifact_ref: str = "scheduler:scheduler_20260521120000_fixed.json",
    producer_run_id: str = "scheduler_20260521120000_fixed",
    producer_schema: str = SCHEDULER_SCHEMA,
    checksum: str | None = None,
    **extra: object,
) -> str:
    receipt_checksum = checksum or "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    payload = json.loads(_bound_proof("scheduler", run_id=run_id))
    payload |= {
        "producer_schema": producer_schema,
        "producer_run_id": producer_run_id,
        "producer_artifact_ref": producer_artifact_ref,
        "scheduler_checksum": receipt_checksum,
        "provenance": {
            "producer_schema": producer_schema,
            "producer_run_id": producer_run_id,
            "producer_artifact_ref": producer_artifact_ref,
            "scheduler_checksum": receipt_checksum,
            "receipt_id": receipt_checksum,
        },
    }
    payload |= extra
    return json.dumps(payload)


def _readiness_cli_args(root: Path, proofs: dict[str, str]) -> list[str]:
    args = ["validate-readiness", "--evidence-root", str(root), "--run-id", "m19"]
    proof_options = {
        "auth_proof": "--auth-proof",
        "alert_proof": "--alert-proof",
        "rollback_proof": "--rollback-proof",
        "slurm_proof": "--slurm-proof",
        "object_store_proof": "--object-store-proof",
        "source_proof": "--source-proof",
        "e2e_proof": "--e2e-proof",
        "mvt_proof": "--mvt-proof",
        "target_env_proof": "--target-env-proof",
    }
    for key, option in proof_options.items():
        if key in proofs:
            args.extend([option, proofs[key]])
    return args


def test_status_execution_mode_truth_table_accepts_allowed_and_rejects_forbidden() -> None:
    for status, modes in ALLOWED_STATUS_EXECUTION_MODES.items():
        for mode in modes:
            validate_readiness_item(_base_item(status, mode))

    for status in ALLOWED_STATUS_EXECUTION_MODES:
        forbidden = EXECUTION_MODE_VALUES - ALLOWED_STATUS_EXECUTION_MODES[status]
        for mode in forbidden:
            with pytest.raises(ProductionReadinessValidationError):
                validate_readiness_item(_base_item(status, mode))

    with pytest.raises(ProductionReadinessValidationError):
        validate_readiness_item(_base_item("not-a-status", "deterministic"))
    with pytest.raises(ProductionReadinessValidationError):
        validate_readiness_item(_base_item("passed", "not-a-mode"))
    missing = _base_item("passed", "deterministic")
    missing.pop("removal_criteria")
    with pytest.raises(ProductionReadinessValidationError):
        validate_readiness_item(missing)


def test_default_readiness_lane_is_deterministic_release_blocked_and_side_effect_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTH_TOKEN", "token=secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")
    root = tmp_path / "artifacts"

    exit_code = slurm_validation.main(["validate-readiness", "--evidence-root", str(root), "--run-id", "m19"])

    assert exit_code == 0
    stdout = capsys.readouterr().out
    assert "super-secret" not in stdout
    assert "token=secret" not in stdout
    rendered_summary = json.loads(stdout)
    assert rendered_summary["status"] == "release_blocked"
    assert rendered_summary["final_production_readiness_claimed"] is False
    assert rendered_summary["live_proof_item_count"] == 0

    items = _items(root)
    deterministic = [item for item in items if item["required_for_final"] is False and item["status"] == "passed"]
    assert deterministic
    assert all(item["execution_mode"] != "live_proof" for item in deterministic)
    required_live = [item for item in items if item["required_for_final"] is True]
    assert required_live
    assert all(item["status"] == "release_blocked" for item in required_live)
    assert all(item["execution_mode"] == "not_executed" for item in required_live)
    assert all(item["removal_criteria"] for item in required_live)
    assert all(item["owner"] for item in required_live)
    assert all(item["action"] for item in required_live)

    preflight = json.loads((root / "m19" / "readiness" / "preflight.json").read_text(encoding="utf-8"))
    policy = preflight["fast_ci_live_side_effect_policy"]
    assert policy == {
        "executes_live_idp": False,
        "executes_live_alert_sink": False,
        "executes_backend_mutation": False,
        "executes_live_rollback": False,
        "executes_live_slurm": False,
        "executes_live_object_store": False,
        "executes_live_weather_source": False,
        "executes_real_national_data": False,
    }

    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir())
    assert "super-secret" not in artifact_text
    assert "token=secret" not in artifact_text


def test_exclusions_are_not_failed_and_do_not_satisfy_live_proof(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19"))

    exclusions = _summary(root)["exclusions"]
    exclusion_ids = {exclusion["id"] for exclusion in exclusions}
    assert {"cldas-restricted", "real-national-data-incomplete"} <= exclusion_ids
    exclusion_items = [item for item in _items(root) if item["exclusions"]]
    assert {item["status"] for item in exclusion_items} == {"not_executed"}
    assert all(item["execution_mode"] == "not_executed" for item in exclusion_items)
    assert all(item["live_proof_accepted"] is False for item in exclusion_items)


def test_incomplete_live_auth_receipt_is_redacted_and_remains_release_blocked(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            auth_proof=_auth_proof(allowed=["models.activate"], denied=[]),
        )
    )

    auth_item = next(item for item in _items(root) if item["surface"] == "live_backend_auth")
    assert auth_item["status"] == "release_blocked"
    assert auth_item["execution_mode"] == "live_proof"
    assert auth_item["live_proof_accepted"] is False
    assert "missing_allowed_actions" in auth_item["details"]["acceptance_errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False

    evidence = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert "super-secret" not in evidence
    assert "token=secret" not in evidence
    assert "user:pass@" not in evidence
    assert "https://idp.example.invalid/auth" in evidence


def test_malformed_and_oversized_live_proofs_are_bounded_release_blockers(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    oversized = "x" * (70 * 1024)
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            auth_proof="{not-json-token=secret",
            alert_proof=oversized,
        )
    )

    items = _items(root)
    auth_item = next(item for item in items if item["surface"] == "live_backend_auth")
    alert_item = next(item for item in items if item["surface"] == "live_alert_sink_delivery")
    assert auth_item["status"] == "release_blocked"
    assert auth_item["execution_mode"] == "live_proof"
    assert alert_item["status"] == "release_blocked"
    assert alert_item["execution_mode"] == "live_proof"

    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert "not-json-token=secret" not in receipts
    assert "[redacted]" in receipts
    assert len(receipts) < 20_000
    assert "Traceback" not in receipts


@pytest.mark.parametrize(
    ("proof_arg", "surface"),
    [
        ("alert_proof", "live_alert_sink_delivery"),
        ("rollback_proof", "live_rollback_execution"),
        ("slurm_proof", "live_slurm_dependency_proof"),
        ("object_store_proof", "live_object_store_dependency_proof"),
        ("source_proof", "live_source_weather_dependency_proof"),
        ("e2e_proof", "live_e2e_dependency_proof"),
        ("mvt_proof", "live_mvt_performance_proof"),
        ("target_env_proof", "target_environment_config_proof"),
    ],
)
@pytest.mark.parametrize("minimal", [{"accepted": True}, {"accepted": True, "status": "passed"}])
def test_minimal_accepted_live_receipts_remain_release_blocked(
    tmp_path: Path,
    proof_arg: str,
    surface: str,
    minimal: dict[str, object],
) -> None:
    root = tmp_path / "artifacts"
    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: json.dumps(minimal)})
    )

    item = next(item for item in _items(root) if item["surface"] == surface)
    assert item["status"] == "release_blocked"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is False
    assert "missing_artifact_or_evidence_refs" in item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("proof_arg", "proof_key", "surface"),
    [
        ("alert_proof", "alert", "live_alert_sink_delivery"),
        ("slurm_proof", "slurm", "live_slurm_dependency_proof"),
        ("target_env_proof", "target_env", "target_environment_config_proof"),
    ],
)
def test_wrong_surface_target_schema_or_deterministic_mode_blocks_live_receipt(
    tmp_path: Path,
    proof_arg: str,
    proof_key: str,
    surface: str,
) -> None:
    root = tmp_path / "artifacts"
    receipt = _bound_proof(
        proof_key,
        schema="nhms.production_readiness.live_proof.v0",
        surface="live_sibling_surface",
        run_id="stale-run",
        target_environment="staging",
        execution_mode="deterministic",
    )

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: receipt}))

    item = next(item for item in _items(root) if item["surface"] == surface)
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert "schema_mismatch" in errors
    assert "surface_mismatch" in errors
    assert "run_id_mismatch" in errors
    assert "target_environment_mismatch" in errors
    assert "execution_mode_not_live_proof" in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_embedded_local_path_values_and_keys_are_redacted_from_receipts_and_stdout(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    unix_path = "/tmp/nhms/private/proof.json"
    windows_path = r"C:\Users\release\secret\proof.json"
    unc_path = r"\\prod-share\release\proof.json"
    file_uri = "file:///var/lib/nhms/receipt.json"

    auth_payload = json.loads(_auth_proof(allowed=["models.activate"], denied=[]))
    auth_payload[unix_path] = "auth key path must be redacted"
    alert_payload = json.loads(
        _bound_proof(
            "alert",
            accepted=False,
            status="failed",
            message=f"failed reading {unix_path} {windows_path} {unc_path} {file_uri}",
        )
    )
    alert_payload[windows_path] = "alert key path must be redacted"
    slurm_payload = json.loads(_bound_proof("slurm", accepted=False, status="failed"))
    slurm_payload[unc_path] = "dependency key path must be redacted"
    target_env_payload = json.loads(_bound_proof("target_env", accepted=False, status="failed"))
    target_env_payload[file_uri] = "target-env key path must be redacted"

    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            "--auth-proof",
            json.dumps(auth_payload),
            "--alert-proof",
            json.dumps(alert_payload),
            "--slurm-proof",
            json.dumps(slurm_payload),
            "--target-env-proof",
            json.dumps(target_env_payload),
        ]
    )

    assert exit_code == 0
    combined = capsys.readouterr().out
    artifact_text = combined + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir()
    )
    for raw_path in (unix_path, windows_path, unc_path, file_uri):
        assert raw_path not in artifact_text
    assert "[redacted-path]" in artifact_text
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("proof_arg", "proof_key", "surface", "override"),
    [
        ("auth_proof", "auth", "live_backend_auth", {"artifact_refs": [None]}),
        ("auth_proof", "auth", "live_backend_auth", {"artifact_refs": []}),
        ("auth_proof", "auth", "live_backend_auth", {"artifact_refs": ["   "]}),
        ("auth_proof", "auth", "live_backend_auth", {"provider": {}, "provider_metadata": {}}),
        ("auth_proof", "auth", "live_backend_auth", {"provider": {"issuer_url": None}}),
        ("auth_proof", "auth", "live_backend_auth", {"role_mapping": {"operator": []}, "role_mappings": {}}),
        ("alert_proof", "alert", "live_alert_sink_delivery", {"sink_metadata": {}}),
        ("alert_proof", "alert", "live_alert_sink_delivery", {"delivery_metadata": {"message_id": None}}),
        ("rollback_proof", "rollback", "live_rollback_execution", {"preconditions": {"backup": None}}),
        ("rollback_proof", "rollback", "live_rollback_execution", {"command_metadata": {"command": ""}}),
        ("target_env_proof", "target_env", "target_environment_config_proof", {"config_metadata": {"cluster": None}}),
    ],
)
def test_vacuous_live_receipt_material_remains_release_blocked(
    tmp_path: Path,
    proof_arg: str,
    proof_key: str,
    surface: str,
    override: dict[str, object],
) -> None:
    root = tmp_path / "artifacts"
    actions = _all_auth_actions()
    receipt = (
        _auth_proof(allowed=actions, denied=actions, **override)
        if proof_key == "auth"
        else _bound_proof(proof_key, **override)
    )

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: receipt}))

    item = next(item for item in _items(root) if item["surface"] == surface)
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"dependency_surface": "object_store"}, "dependency_surface_mismatch"),
        ({"producer_issue": 148}, "producer_issue_mismatch"),
        ({"producer_schema": "nhms.production_closure.object_store.v1"}, "producer_schema_mismatch"),
        (
            {
                "producer_run_id": "",
                "provenance": {
                    "dependency": "slurm",
                    "producer_issue": 147,
                    "producer_schema": "nhms.production_closure.slurm.v1",
                    "producer_artifact_ref": "slurm:summary.json",
                    "summary_checksum": "sha256:abc123",
                },
            },
            "missing_producer_run_id",
        ),
        (
            {
                "producer_artifact_ref": "",
                "provenance": {
                    "dependency": "slurm",
                    "producer_issue": 147,
                    "producer_schema": "nhms.production_closure.slurm.v1",
                    "producer_run_id": "slurm-live-run",
                    "summary_checksum": "sha256:abc123",
                },
            },
            "missing_producer_artifact_ref",
        ),
        (
            {
                "summary_checksum": "",
                "producer_checksum": "",
                "provenance": {
                    "dependency": "slurm",
                    "producer_issue": 147,
                    "producer_schema": "nhms.production_closure.slurm.v1",
                    "producer_run_id": "slurm-live-run",
                    "producer_artifact_ref": "slurm:summary.json",
                },
            },
            "missing_producer_checksum_or_receipt_id",
        ),
        ({"provenance": {"note": "placeholder"}}, "placeholder_provenance"),
    ],
)
def test_dependency_receipts_require_semantic_producer_provenance(
    tmp_path: Path,
    override: dict[str, object],
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    receipt = _bound_proof("slurm", **override)

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_proof=receipt))

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert expected_error in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"producer_run_id": "stale-run"}, "producer_run_id_mismatch"),
        ({"producer_artifact_ref": "slurm:sibling-summary.json"}, "producer_artifact_ref_mismatch"),
        ({"summary_checksum": "sha256:stale"}, "producer_checksum_mismatch"),
        (
            {
                "dependency_surface": "object_store",
                "producer_issue": 148,
                "producer_schema": "nhms.production_closure.object_store.v1",
                "producer_run_id": "object_store-live-run",
                "producer_artifact_ref": "object_store:summary.json",
                "summary_checksum": "sha256:sibling",
            },
            "dependency_surface_mismatch",
        ),
    ],
)
def test_dependency_receipts_bind_to_consumed_producer_summary(
    tmp_path: Path,
    override: dict[str, object],
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    slurm_root = tmp_path / "slurm"
    receipt = _dependency_proof_bound_to_summary("slurm", slurm_root, **override)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=slurm_root,
            slurm_proof=receipt,
        )
    )

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert expected_error in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize("proof_key", sorted(DEPENDENCY_PROOFS))
def test_dependency_receipts_reject_sibling_nested_provenance(
    tmp_path: Path,
    proof_key: str,
) -> None:
    root = tmp_path / "artifacts"
    producer_root = tmp_path / proof_key
    sibling = "object_store" if proof_key != "object_store" else "slurm"
    sibling_issue, sibling_schema = DEPENDENCY_CONTRACTS[sibling]
    proof_arg = f"{proof_key}_proof" if proof_key != "object_store" else "object_store_proof"
    evidence_root_arg = f"{proof_key}_evidence_root" if proof_key != "object_store" else "object_store_evidence_root"
    receipt = _dependency_proof_bound_to_summary(
        proof_key,
        producer_root,
        provenance={
            "dependency": sibling,
            "producer_issue": sibling_issue,
            "producer_schema": sibling_schema,
            "producer_run_id": f"{sibling}-live-run",
            "producer_artifact_ref": f"{sibling}:summary.json",
            "summary_checksum": "sha256:sibling",
            "receipt_id": "sha256:sibling",
        },
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            **{evidence_root_arg: producer_root, proof_arg: receipt},
        )
    )

    item = next(item for item in _items(root) if item["surface"] == PROOF_SURFACES[proof_key])
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert "provenance_dependency_mismatch" in errors
    assert "provenance_producer_issue_mismatch" in errors
    assert "provenance_producer_schema_mismatch" in errors
    assert "provenance_producer_run_id_mismatch" in errors
    assert "provenance_producer_artifact_ref_mismatch" in errors
    assert "provenance_producer_checksum_or_receipt_id_mismatch" in errors
    assert "provenance_summary_producer_run_id_mismatch" in errors
    assert "provenance_summary_producer_artifact_ref_mismatch" in errors
    assert "provenance_summary_producer_checksum_mismatch" in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"dependency_surface": "slurm", "dependency_name": "object_store"}, "top_level_dependency_alias_mismatch"),
        ({"producer_issue": 147, "summary_issue": 148}, "top_level_producer_issue_alias_mismatch"),
        (
            {
                "producer_schema": "nhms.production_closure.slurm.v1",
                "summary_schema": "nhms.production_closure.object_store.v1",
            },
            "top_level_producer_schema_alias_mismatch",
        ),
        (
            {"producer_run_id": "slurm-live-run", "summary_run_id": "object_store-live-run"},
            "top_level_producer_run_id_alias_mismatch",
        ),
        (
            {"producer_artifact_ref": "slurm:summary.json", "summary_ref": "object_store:summary.json"},
            "top_level_producer_artifact_ref_alias_mismatch",
        ),
        (
            {"summary_checksum": "sha256:abc123", "producer_checksum": "sha256:object-store-sibling"},
            "top_level_producer_checksum_or_receipt_id_alias_mismatch",
        ),
    ],
)
def test_dependency_receipts_reject_contradictory_lower_priority_top_level_aliases(
    tmp_path: Path,
    override: dict[str, object],
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    receipt = _bound_proof("slurm", **override)

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_proof=receipt))

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert expected_error in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("provenance_override", "expected_error"),
    [
        ({"dependency": "slurm", "dependency_name": "object_store"}, "provenance_dependency_alias_mismatch"),
        ({"producer_issue": 147, "summary_issue": 148}, "provenance_producer_issue_alias_mismatch"),
        (
            {
                "producer_schema": "nhms.production_closure.slurm.v1",
                "summary_schema": "nhms.production_closure.object_store.v1",
            },
            "provenance_producer_schema_alias_mismatch",
        ),
        (
            {"producer_run_id": "slurm-live-run", "summary_run_id": "object_store-live-run"},
            "provenance_producer_run_id_alias_mismatch",
        ),
        (
            {"producer_artifact_ref": "slurm:summary.json", "summary_ref": "object_store:summary.json"},
            "provenance_producer_artifact_ref_alias_mismatch",
        ),
        (
            {"summary_checksum": "sha256:abc123", "producer_checksum": "sha256:object-store-sibling"},
            "provenance_producer_checksum_or_receipt_id_alias_mismatch",
        ),
    ],
)
def test_dependency_receipts_reject_contradictory_lower_priority_provenance_aliases(
    tmp_path: Path,
    provenance_override: dict[str, object],
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    payload = json.loads(_bound_proof("slurm"))
    payload["provenance"] |= provenance_override

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_proof=json.dumps(payload))
    )

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert expected_error in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_dependency_receipt_accepts_agreeing_top_level_provenance_and_summary(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    slurm_root = tmp_path / "slurm"
    receipt = _dependency_proof_bound_to_summary("slurm", slurm_root)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=slurm_root,
            slurm_proof=receipt,
        )
    )

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    assert item["status"] == "passed"
    assert item["live_proof_accepted"] is True
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_dry_run_evidence_ingests_as_deterministic_non_final_review_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(scheduler_path)

    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            "--scheduler-evidence-file",
            str(scheduler_path),
        ]
    )

    assert exit_code == 0
    rendered_summary = json.loads(capsys.readouterr().out)
    assert rendered_summary["final_production_readiness_claimed"] is False
    item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    assert item["status"] == "passed"
    assert item["execution_mode"] == "deterministic"
    assert item["required_for_final"] is False
    assert item["live_proof_accepted"] is False
    assert item["details"]["scheduler_pass_id"] == "scheduler_20260521120000_fixed"
    assert item["details"]["scheduler_execution_mode"] == "dry_run"
    assert item["details"]["scheduler_artifact_ref"] == "scheduler:scheduler_20260521120000_fixed.json"
    assert item["details"]["scheduler_checksum"].startswith("sha256:")

    live_scheduler_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert live_scheduler_item["status"] == "release_blocked"
    assert live_scheduler_item["execution_mode"] == "not_executed"
    assert live_scheduler_item["required_for_final"] is True
    assert live_scheduler_item["live_proof_accepted"] is False
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_live_receipt_accepts_only_when_bound_to_consumed_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(scheduler_path, status="submitted", execution_mode="production_orchestration")

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=_scheduler_proof_bound_to_evidence(scheduler_path),
        )
    )

    scheduler_review = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert scheduler_review["status"] == "passed"
    assert scheduler_review["live_proof_accepted"] is False
    assert live_item["status"] == "passed"
    assert live_item["execution_mode"] == "live_proof"
    assert live_item["live_proof_accepted"] is True
    assert live_item["details"]["payload"]["producer_artifact_ref"] == "scheduler:scheduler_20260521120000_fixed.json"
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("override", "expected_error"),
    [
        ({"schema": "nhms.production_readiness.live_proof.v0"}, "schema_mismatch"),
        ({"run_id": "stale-run"}, "run_id_mismatch"),
        ({"target_environment": "staging"}, "target_environment_mismatch"),
        ({"execution_mode": "deterministic"}, "execution_mode_not_live_proof"),
        ({"producer_schema": "nhms.production_scheduler.pass_evidence.v0"}, "producer_schema_mismatch"),
        ({"producer_run_id": "stale-pass"}, "producer_run_id_mismatch"),
        ({"producer_artifact_ref": "scheduler:sibling.json"}, "producer_artifact_ref_mismatch"),
        ({"scheduler_checksum": "sha256:stale"}, "producer_checksum_mismatch"),
        (
            {"scheduler_checksum": "", "provenance": {"producer_schema": SCHEDULER_SCHEMA}},
            "missing_producer_checksum_or_receipt_id",
        ),
    ],
)
def test_scheduler_live_receipt_rejects_stale_or_identity_mismatched_binding(
    tmp_path: Path,
    override: dict[str, object],
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(scheduler_path)
    receipt = _scheduler_proof_bound_to_evidence(scheduler_path, **override)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is False
    assert expected_error in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("writer", "expected_error"),
    [
        (
            lambda path: path.write_text("{not-json token=secret", encoding="utf-8"),
            "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED",
        ),
        (
            lambda path: path.write_text(
                json.dumps(_scheduler_evidence_payload(stale=True)),
                encoding="utf-8",
            ),
            "stale_scheduler_evidence",
        ),
        (
            lambda path: path.write_text(
                json.dumps(_scheduler_evidence_payload(pass_id="../private-scheduler-pass")),
                encoding="utf-8",
            ),
            "unsafe_scheduler_identity",
        ),
        (
            lambda path: path.write_text(
                json.dumps(_scheduler_evidence_payload(final_claimed=True)),
                encoding="utf-8",
            ),
            "scheduler_evidence_claimed_final_readiness",
        ),
    ],
)
def test_scheduler_malformed_stale_or_unsafe_evidence_is_blocked_and_redacted(
    tmp_path: Path,
    writer: object,
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "private" / "scheduler" / "scheduler_20260521120000_fixed.json"
    scheduler_path.parent.mkdir(parents=True)
    writer(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["live_proof_accepted"] is False
    rendered = json.dumps(item, sort_keys=True)
    assert expected_error in rendered
    assert str(scheduler_path.parent) not in rendered
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_oversized_scheduler_evidence_is_bounded_blocked_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    scheduler_path.parent.mkdir(parents=True)
    scheduler_path.write_text(json.dumps(_scheduler_evidence_payload(blob="x" * (260 * 1024))), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    assert item["status"] == "blocked"
    assert item["details"]["error_code"] == "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_TOO_LARGE"
    artifacts = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir())
    assert len(artifacts) < 80_000
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_existing_m19_dependency_summary_truth_table_unchanged_when_scheduler_absent(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    lanes = {
        "slurm": ("nhms.production_closure.slurm.v1", 147, "ready"),
        "object-store": ("nhms.production_closure.object_store.v1", 148, "ready"),
        "source": ("nhms.production_closure.met.v1", 149, "ready"),
        "e2e": ("nhms.production_closure.e2e.v1", 150, "ready"),
        "mvt": ("nhms.production_closure.scale.v1", 151, "ready"),
    }
    for lane, (schema, issue, status) in lanes.items():
        lane_root = tmp_path / lane
        lane_root.mkdir()
        (lane_root / "summary.json").write_text(
            json.dumps(
                {
                    "schema": schema,
                    "issue": issue,
                    "run_id": f"{lane}-run",
                    "status": status,
                    "execution_mode": "deterministic_fixture",
                    "final_production_readiness_claimed": False,
                }
            ),
            encoding="utf-8",
        )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=tmp_path / "slurm",
            object_store_evidence_root=tmp_path / "object-store",
            source_evidence_root=tmp_path / "source",
            e2e_evidence_root=tmp_path / "e2e",
            mvt_evidence_root=tmp_path / "mvt",
        )
    )

    items = _items(root)
    consumed = [item for item in items if item["surface"].endswith("_production_like_evidence")]
    assert len(consumed) == 5
    assert {item["surface"] for item in consumed} == {
        "slurm_production_like_evidence",
        "object_store_production_like_evidence",
        "source_production_like_evidence",
        "e2e_production_like_evidence",
        "mvt_production_like_evidence",
    }
    assert all(item["status"] == "passed" for item in consumed)
    assert all(item["execution_mode"] == "deterministic" for item in consumed)
    assert all(item["live_proof_accepted"] is False for item in consumed)
    assert "live_scheduler_evidence_proof" not in {item["surface"] for item in items}
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_dependency_receipt_accepts_all_agreeing_aliases(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    slurm_root = tmp_path / "slurm"
    checksum = _write_dependency_summary(slurm_root, "slurm")
    payload = json.loads(_bound_proof("slurm"))
    aliases = {
        "dependency_surface": "slurm",
        "dependency_name": "slurm",
        "dependency": "slurm",
        "producer_issue": 147,
        "summary_issue": "#147",
        "producer_schema": "nhms.production_closure.slurm.v1",
        "summary_schema": "nhms.production_closure.slurm.v1",
        "producer_run_id": "slurm-live-run",
        "summary_run_id": "slurm-live-run",
        "producer_artifact_ref": "slurm:summary.json",
        "producer_artifact_path": "slurm:summary.json",
        "producer_artifact_uri": "slurm:summary.json",
        "summary_ref": "slurm:summary.json",
        "summary_path": "slurm:summary.json",
        "artifact_ref": "slurm:summary.json",
        "artifact_path": "slurm:summary.json",
        "artifact_uri": "slurm:summary.json",
        "summary_checksum": checksum,
        "producer_checksum": checksum,
        "checksum": checksum,
        "digest": checksum,
        "producer_receipt_id": checksum,
        "receipt_id": checksum,
    }
    payload |= aliases
    payload["provenance"] = dict(aliases)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=slurm_root,
            slurm_proof=json.dumps(payload),
        )
    )

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    assert item["status"] == "passed"
    assert item["live_proof_accepted"] is True


@pytest.mark.parametrize(
    ("top_level_path", "provenance_path"),
    [
        ("/tmp/private-slurm-alias/summary.json", "/tmp/private-object-store-alias/summary.json"),
        (r"C:\nhms\private-slurm-alias\summary.json", r"D:\nhms\private-object-store-alias\summary.json"),
        (r"\\prod-share\private-slurm-alias\summary.json", r"\\prod-share\private-object-store-alias\summary.json"),
        (
            "file:///var/lib/nhms/private-slurm-alias/summary.json",
            "file:///var/lib/nhms/private-object-store-alias/summary.json",
        ),
    ],
)
def test_dependency_receipts_validate_raw_path_aliases_before_public_redaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    top_level_path: str,
    provenance_path: str,
) -> None:
    root = tmp_path / "artifacts"
    proofs = _all_live_proofs()
    proofs["slurm_proof"] = _dependency_proof_with_artifact_paths(
        "slurm",
        top_level_path=top_level_path,
        provenance_path=provenance_path,
    )

    exit_code = slurm_validation.main(_readiness_cli_args(root, proofs))

    assert exit_code == 0
    stdout = capsys.readouterr().out
    rendered_summary = json.loads(stdout)
    assert rendered_summary["status"] == "release_blocked"
    assert rendered_summary["final_production_readiness_claimed"] is False

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    errors = item["details"]["acceptance_errors"]["errors"]
    assert item["status"] == "release_blocked"
    assert item["live_proof_accepted"] is False
    assert "provenance_producer_artifact_ref_mismatch" in errors
    assert _summary(root)["final_production_readiness_claimed"] is False

    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").glob("*.json"))
    for raw_path in (top_level_path, provenance_path):
        assert raw_path not in stdout
        assert raw_path not in artifact_text
    for raw_marker in ("private-slurm-alias", "private-object-store-alias"):
        assert raw_marker not in stdout
        assert raw_marker not in artifact_text
    assert "[redacted-path]" in artifact_text
    assert "raw_payload" not in artifact_text


def test_dependency_receipt_accepts_agreeing_raw_path_aliases_and_redacts_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    raw_path = "/tmp/private-slurm-alias/summary.json"
    proofs = _all_live_proofs()
    proofs["slurm_proof"] = _dependency_proof_with_artifact_paths(
        "slurm",
        top_level_path=raw_path,
        provenance_path=raw_path,
    )

    exit_code = slurm_validation.main(_readiness_cli_args(root, proofs))

    assert exit_code == 0
    stdout = capsys.readouterr().out
    rendered_summary = json.loads(stdout)
    assert rendered_summary["status"] == "ready"
    assert rendered_summary["final_production_readiness_claimed"] is True

    item = next(item for item in _items(root) if item["surface"] == "live_slurm_dependency_proof")
    assert item["status"] == "passed"
    assert item["live_proof_accepted"] is True

    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").glob("*.json"))
    assert raw_path not in stdout
    assert raw_path not in artifact_text
    assert "private-slurm-alias" not in stdout
    assert "private-slurm-alias" not in artifact_text
    assert "[redacted-path]" in artifact_text
    assert "raw_payload" not in artifact_text


def test_dependency_summary_missing_root_redacts_paths_from_stdout_and_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    private_root = tmp_path / "private" / "slurm-evidence"

    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            "--slurm-evidence-root",
            str(private_root),
        ]
    )

    assert exit_code == 0
    artifact_text = capsys.readouterr().out + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir()
    )
    assert str(private_root) not in artifact_text
    assert "[redacted-path]" in artifact_text
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_readiness_cli_existing_lane_error_redacts_absolute_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "private" / "artifacts"
    lane = root / "m19" / "readiness"
    lane.mkdir(parents=True)
    (lane / "existing.txt").write_text("existing", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        slurm_validation.main(["validate-readiness", "--evidence-root", str(root), "--run-id", "m19"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert str(root) not in rendered
    assert str(lane) not in rendered
    assert "PRODUCTION_READINESS_EVIDENCE_EXISTS" in rendered
    assert "[redacted-path]" in rendered


def test_readiness_cli_symlink_evidence_component_error_redacts_absolute_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-artifacts"
    real_root.mkdir()
    symlink_root = tmp_path / "private-symlink-artifacts"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(SystemExit) as exc_info:
        slurm_validation.main(["validate-readiness", "--evidence-root", str(symlink_root), "--run-id", "m19"])

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert str(symlink_root) not in rendered
    assert "PRODUCTION_READINESS_EVIDENCE_SYMLINK" in rendered
    assert "[redacted-path]" in rendered


@pytest.mark.parametrize("proof_arg", ["auth_proof", "alert_proof"])
def test_deep_live_proof_json_is_stable_release_blocker(tmp_path: Path, proof_arg: str) -> None:
    root = tmp_path / "artifacts"
    deep_json = "[" * 20000 + "0" + "]" * 20000
    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", **{proof_arg: deep_json}))

    surface = "live_backend_auth" if proof_arg == "auth_proof" else "live_alert_sink_delivery"
    item = next(item for item in _items(root) if item["surface"] == surface)
    assert item["status"] == "release_blocked"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is False
    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert len(receipts) < 20_000
    assert "Traceback" not in receipts
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_all_live_receipts_accepted_claims_final_readiness(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    slurm_root = tmp_path / "slurm"
    object_store_root = tmp_path / "object-store"
    source_root = tmp_path / "source"
    e2e_root = tmp_path / "e2e"
    mvt_root = tmp_path / "mvt"
    proofs = _all_live_proofs()
    proofs |= {
        "slurm_proof": _dependency_proof_bound_to_summary("slurm", slurm_root),
        "object_store_proof": _dependency_proof_bound_to_summary("object_store", object_store_root),
        "source_proof": _dependency_proof_bound_to_summary("source", source_root),
        "e2e_proof": _dependency_proof_bound_to_summary("e2e", e2e_root),
        "mvt_proof": _dependency_proof_bound_to_summary("mvt", mvt_root),
    }
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=slurm_root,
            object_store_evidence_root=object_store_root,
            source_evidence_root=source_root,
            e2e_evidence_root=e2e_root,
            mvt_evidence_root=mvt_root,
            **proofs,
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is True
    assert summary["status"] == "ready"
    assert summary["release_blockers"] == []
    assert summary["accepted_live_proof_count"] == summary["required_live_proof_count"] == 9


def test_any_required_live_blocker_keeps_final_readiness_false_and_lists_blocker(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    proofs = _all_live_proofs()
    proofs.pop("target_env_proof")
    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            **proofs,
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is False
    blockers = _blockers(root)
    target_blocker = next(blocker for blocker in blockers if blocker["surface"] == "target_environment_config_proof")
    assert target_blocker["blocker_id"] == "m19-live-target-environment-config"
    assert target_blocker["residual_risk"]
    assert target_blocker["removal_criteria"]
    assert target_blocker["artifact_refs"] == ["live_proof_receipts.json"]


def test_consumes_existing_lane_summaries_without_changing_final_live_gate(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    lanes = {
        "slurm": ("nhms.production_closure.slurm.v1", 147, "ready"),
        "object-store": ("nhms.production_closure.object_store.v1", 148, "ready"),
        "source": ("nhms.production_closure.met.v1", 149, "ready"),
        "e2e": ("nhms.production_closure.e2e.v1", 150, "ready"),
        "mvt": ("nhms.production_closure.scale.v1", 151, "ready"),
    }
    for lane, (schema, issue, status) in lanes.items():
        lane_root = tmp_path / lane
        lane_root.mkdir()
        (lane_root / "summary.json").write_text(
            json.dumps(
                {
                    "schema": schema,
                    "issue": issue,
                    "run_id": f"{lane}-run",
                    "status": status,
                    "execution_mode": "deterministic_fixture",
                    "final_production_readiness_claimed": False,
                }
            ),
            encoding="utf-8",
        )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            slurm_evidence_root=tmp_path / "slurm",
            object_store_evidence_root=tmp_path / "object-store",
            source_evidence_root=tmp_path / "source",
            e2e_evidence_root=tmp_path / "e2e",
            mvt_evidence_root=tmp_path / "mvt",
        )
    )

    summary = _summary(root)
    assert summary["final_production_readiness_claimed"] is False
    consumed = [item for item in _items(root) if item["surface"].endswith("_production_like_evidence")]
    assert len(consumed) == 5
    assert all(item["status"] == "passed" for item in consumed)
    assert all(item["execution_mode"] == "deterministic" for item in consumed)
    assert all(item["live_proof_accepted"] is False for item in consumed)


def test_deep_dependency_summary_json_is_stable_blocked_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "slurm"
    summary_root.mkdir()
    (summary_root / "summary.json").write_text("[" * 20000 + "0" + "]" * 20000, encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["live_proof_accepted"] is False
    artifacts = "\n".join(path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir())
    assert "Traceback" not in artifacts
    assert _summary(root)["final_production_readiness_claimed"] is False
