from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest

from services.orchestrator.chain import PipelineResult, StageRunResult
from services.orchestrator.scheduler import ProductionScheduler
from services.production_closure import slurm_validation
from services.production_closure.readiness_validation import (
    ALLOWED_STATUS_EXECUTION_MODES,
    EXECUTION_MODE_VALUES,
    MAX_SCHEDULER_EVIDENCE_FILES,
    ProductionReadinessConfig,
    ProductionReadinessValidationError,
    validate_readiness,
    validate_readiness_item,
)
from tests.test_production_scheduler import (
    FakeAdapter,
    FakeProductionOrchestrator,
    FakeRegistry,
)
from tests.test_production_scheduler import (
    _config as _scheduler_config,
)
from tests.test_production_scheduler import (
    _dt as _scheduler_dt,
)
from tests.test_production_scheduler import (
    _model as _scheduler_model,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time

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
                "scenario_id": "forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "forcing_version_id": "forc_gfs_2026052106_model_a",
            }
        ],
        "blocked_candidates": [],
        "skipped_candidates": [
            {
                "candidate_id": "IFS:2026-05-21T06:00:00Z:model_a:forecast_ifs_deterministic",
                "source_id": "IFS",
                "cycle_time_utc": "2026-05-21T06:00:00Z",
                "model_id": "model_a",
                "scenario_id": "forecast_ifs_deterministic",
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


def _write_scheduler_payload(path: Path, payload: dict[str, object]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_for_model(model_id: str) -> dict[str, object]:
    return {
        "candidate_id": f"gfs:2026-05-21T06:00:00Z:{model_id}:forecast_gfs_deterministic",
        "source_id": "gfs",
        "cycle_time_utc": "2026-05-21T06:00:00Z",
        "model_id": model_id,
        "scenario_id": "forecast_gfs_deterministic",
        "run_id": f"fcst_gfs_2026052106_{model_id}",
        "forcing_version_id": f"forc_gfs_2026052106_{model_id}",
    }


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
    _write_scheduler_payload(scheduler_path, _submitted_scheduler_payload())

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
    assert scheduler_review["details"]["submitted_count"] == 1
    assert live_item["status"] == "passed"
    assert live_item["execution_mode"] == "live_proof"
    assert live_item["live_proof_accepted"] is True
    assert live_item["details"]["payload"]["producer_artifact_ref"] == "scheduler:scheduler_20260521120000_fixed.json"
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_submitted_noop_production_evidence_blocks_live_binding(tmp_path: Path) -> None:
    payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "submitted_status_without_model_run_evidence" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize("status", ["planned", "ready"])
def test_scheduler_planned_or_ready_production_evidence_cannot_accept_live_binding(
    tmp_path: Path,
    status: str,
) -> None:
    payload = _scheduler_evidence_payload(status=status, execution_mode="production_orchestration")

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "passed"
    assert live_item["status"] == "release_blocked"
    assert "scheduler_status_not_live_eligible" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize("status", ["completed", "succeeded"])
def test_scheduler_completed_or_succeeded_noop_production_evidence_blocks_live_binding(
    tmp_path: Path,
    status: str,
) -> None:
    payload = _scheduler_evidence_payload(status=status, execution_mode="production_orchestration")

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "submitted_status_without_model_run_evidence" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_live_submitted_row_without_status_evidence_blocks_live_binding(tmp_path: Path) -> None:
    payload = _submitted_scheduler_payload()
    payload["model_run_evidence"][0].pop("status")

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "submitted_status_model_run_status_mismatch" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_live_submitted_false_row_blocks_live_binding(tmp_path: Path) -> None:
    payload = _submitted_scheduler_payload()
    payload["model_run_evidence"][0]["status"] = "queued"
    payload["model_run_evidence"][0]["submitted"] = False

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "submitted_count_model_run_evidence_mismatch" in scheduler_item["details"]["acceptance_errors"]
    assert "submitted_status_model_run_status_mismatch" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_schemaless_scheduler_evidence_blocks_live_scheduler_proof(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")
    payload.pop("schema")
    _write_scheduler_payload(scheduler_path, payload)
    receipt = _scheduler_proof_bound_to_evidence(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert scheduler_item["status"] == "blocked"
    assert "schema_mismatch" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "execution_mode",
    [
        "dry_run",
        "deterministic",
        "deterministic_fixture",
        "planning_only",
        "production_like",
        "simulated",
    ],
)
def test_scheduler_live_receipt_rejects_non_live_producer_modes(
    tmp_path: Path,
    execution_mode: str,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(scheduler_path, status="submitted", execution_mode=execution_mode)
    receipt = _scheduler_proof_bound_to_evidence(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert scheduler_item["status"] == "passed"
    assert live_item["status"] == "release_blocked"
    assert live_item["execution_mode"] == "live_proof"
    assert "scheduler_execution_mode_not_live_eligible" in live_item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_all_other_live_receipts_accepted_with_scheduler_bound_to_dry_run_keeps_final_false(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(scheduler_path, status="submitted", execution_mode="dry_run")
    proofs = _all_live_proofs()
    proofs["scheduler_proof"] = _scheduler_proof_bound_to_evidence(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            **proofs,
        )
    )

    required_items = [item for item in _items(root) if item["required_for_final"]]
    blocked = [item for item in required_items if item["status"] == "release_blocked"]
    assert [item["surface"] for item in blocked] == ["live_scheduler_evidence_proof"]
    assert "scheduler_execution_mode_not_live_eligible" in blocked[0]["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_root_binds_receipt_to_exact_matching_passed_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler"
    first = scheduler_root / "first.json"
    second = scheduler_root / "second.json"
    _write_scheduler_payload(first, _submitted_scheduler_payload_with_pass_id("scheduler_20260521120000_first"))
    _write_scheduler_payload(second, _submitted_scheduler_payload_with_pass_id("scheduler_20260521120000_second"))
    receipt = _scheduler_proof_bound_to_evidence(
        second,
        producer_artifact_ref="scheduler:second.json",
        producer_run_id="scheduler_20260521120000_second",
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_root=scheduler_root,
            scheduler_proof=receipt,
        )
    )

    scheduler_items = [item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence"]
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert len([item for item in scheduler_items if item["status"] == "passed"]) == 2
    assert live_item["status"] == "passed"
    assert live_item["live_proof_accepted"] is True


def test_scheduler_root_same_pass_and_checksum_with_distinct_artifact_ref_is_not_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler"
    first = scheduler_root / "first.json"
    second = scheduler_root / "second.json"
    checksum = _write_scheduler_payload(
        first,
        _submitted_scheduler_payload_with_pass_id("scheduler_20260521120000_same"),
    )
    second.parent.mkdir(parents=True, exist_ok=True)
    second.write_bytes(first.read_bytes())
    receipt = _scheduler_proof_bound_to_evidence(
        second,
        producer_artifact_ref="scheduler:second.json",
        producer_run_id="scheduler_20260521120000_same",
        checksum=checksum,
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_root=scheduler_root,
            scheduler_proof=receipt,
        )
    )

    scheduler_items = [item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence"]
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert len([item for item in scheduler_items if item["status"] == "passed"]) == 2
    assert live_item["status"] == "passed"
    assert live_item["live_proof_accepted"] is True
    assert "acceptance_errors" not in live_item["details"]


def test_scheduler_root_duplicate_exact_match_blocks_ambiguous_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler"
    first = scheduler_root / "first.json"
    duplicate = scheduler_root / "duplicate.json"
    monkeypatch.setattr(
        "services.production_closure.readiness_validation._scheduler_evidence_artifact_ref",
        lambda path, *, config: "scheduler:same.json",
    )
    checksum = _write_scheduler_payload(
        first,
        _submitted_scheduler_payload_with_pass_id("scheduler_20260521120000_same"),
    )
    duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_bytes(first.read_bytes())
    receipt = _scheduler_proof_bound_to_evidence(
        duplicate,
        producer_artifact_ref="scheduler:same.json",
        producer_run_id="scheduler_20260521120000_same",
        checksum=checksum,
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_root=scheduler_root,
            scheduler_proof=receipt,
        )
    )

    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert live_item["status"] == "release_blocked"
    assert "ambiguous_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("overflow", "expected_error"),
    [
        ("node", "json_node_limit_exceeded"),
        ("depth", "json_depth_limit_exceeded"),
    ],
)
def test_live_proof_json_traversal_limits_are_release_blockers_and_bounded(
    tmp_path: Path,
    overflow: str,
    expected_error: str,
) -> None:
    root = tmp_path / "artifacts"
    payload = json.loads(_bound_proof("alert"))
    payload["sink_metadata"]["webhook_url"] = "https://user:pass@alerts.example.invalid/hook?token=secret"
    payload["local_path"] = "/tmp/private-live-proof/receipt.json"
    if overflow == "node":
        payload["node_overflow"] = [{"i": index} for index in range(1300)]
    else:
        nested: dict[str, object] = {"value": "too deep"}
        for index in range(20):
            nested = {f"level_{index}": nested}
        payload["depth_overflow"] = nested
    proof = json.dumps(payload)
    assert len(proof.encode("utf-8")) < 64 * 1024

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", alert_proof=proof))

    item = next(item for item in _items(root) if item["surface"] == "live_alert_sink_delivery")
    assert item["status"] == "release_blocked"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is False
    assert item["details"]["parse_status"] == "json_limit_exceeded"
    assert item["details"]["error_code"] == "PRODUCTION_READINESS_PROOF_JSON_LIMIT_EXCEEDED"
    assert expected_error in item["details"]["json_limit_errors"]
    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    assert "[truncated:max-" in receipts
    assert "token=secret" not in receipts
    assert "user:pass@" not in receipts
    assert "/tmp/private-live-proof/receipt.json" not in receipts
    assert "raw_payload" not in receipts
    assert len(receipts.encode("utf-8")) < 64 * 1024
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_candidate_count_requires_identity_collection(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    payload = _scheduler_evidence_payload()
    payload["candidates"] = []
    payload["blocked_candidates"] = []
    payload["skipped_candidates"] = []
    payload["model_run_evidence"] = []
    _write_scheduler_payload(scheduler_path, payload)

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    assert item["status"] == "blocked"
    assert "missing_scheduler_candidate_identity" in item["details"]["acceptance_errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_candidate_and_model_run_identity_mismatch_blocks_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")
    payload["counts"]["candidate_count"] = 1
    payload["counts"]["skipped_candidate_count"] = 0
    payload["counts"]["submitted_count"] = 1
    payload["candidates"] = [
        {
            "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "source_id": "IFS",
            "cycle_time_utc": "2026-05-21T06:00:00Z",
            "model_id": "model_a",
            "scenario_id": "forecast_gfs_deterministic",
            "run_id": "fcst_gfs_2026052106_model_a",
            "forcing_version_id": "forc_gfs_2026052106_model_a",
        }
    ]
    payload["skipped_candidates"] = []
    payload["model_run_evidence"] = [
        {
            "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "source_id": "gfs",
            "cycle_time_utc": "2026-05-21T06:00:00Z",
            "model_id": "model_a",
            "scenario_id": "forecast_gfs_deterministic",
            "run_id": "fcst_gfs_2026052106_model_a",
            "forcing_version_id": "forc_gfs_2026052106_model_a",
            "hydro_run": {"run_id": "fcst_sibling"},
            "forcing": {"forcing_version_id": "forc_sibling"},
        }
    ]
    _write_scheduler_payload(scheduler_path, payload)

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    errors = item["details"]["acceptance_errors"]
    assert item["status"] == "blocked"
    assert "candidates_candidate_id_identity_mismatch" in errors
    assert "model_run_evidence_source_id_identity_mismatch" in errors
    assert "model_run_evidence_run_id_mismatch" in errors
    assert "model_run_evidence_forcing_version_id_mismatch" in errors
    assert _summary(root)["final_production_readiness_claimed"] is False


def _validate_scheduler_payload_with_matching_live_proof(
    tmp_path: Path,
    payload: dict[str, object],
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_payload(scheduler_path, payload)
    receipt = _scheduler_proof_bound_to_evidence(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    return _summary(root), scheduler_item, live_item


def _submitted_scheduler_payload() -> dict[str, object]:
    payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")
    payload["counts"]["candidate_count"] = 1
    payload["counts"]["skipped_candidate_count"] = 0
    payload["counts"]["submitted_count"] = 1
    payload["skipped_candidates"] = []
    payload["model_run_evidence"] = [
        {
            **_candidate_for_model("model_a"),
            "status": "submitted",
            "submitted": True,
        }
    ]
    return payload


def _submitted_scheduler_payload_with_pass_id(pass_id: str) -> dict[str, object]:
    payload = _submitted_scheduler_payload()
    payload["pass_id"] = pass_id
    return payload


def _partial_scheduler_payload() -> dict[str, object]:
    payload = _submitted_scheduler_payload()
    payload["status"] = "submitted_partial"
    payload["counts"]["partial_count"] = 1
    payload["model_run_evidence"][0]["status"] = "partial"
    payload["model_run_evidence"][0]["candidate_outcome"] = {"status": "partial"}
    return payload


def _failed_scheduler_payload(status: str = "failed") -> dict[str, object]:
    payload = _submitted_scheduler_payload()
    payload["status"] = "failed"
    payload["counts"]["failed_count"] = 1
    payload["model_run_evidence"][0]["status"] = status
    payload["model_run_evidence"][0]["candidate_outcome"] = {"status": status}
    return payload


def _two_model_submitted_scheduler_payload() -> dict[str, object]:
    payload = _submitted_scheduler_payload()
    second_candidate = _candidate_for_model("model_b")
    payload["counts"]["candidate_count"] = 2
    payload["counts"]["submitted_count"] = 2
    payload["candidates"].append(second_candidate)
    payload["model_run_evidence"].append({**second_candidate, "status": "submitted", "submitted": True})
    return payload


def _submitted_partial_scheduler_payload_with_failed_sibling() -> dict[str, object]:
    payload = _two_model_submitted_scheduler_payload()
    payload["status"] = "submitted_partial"
    payload["counts"]["failed_count"] = 1
    payload["counts"]["partial_count"] = 1
    payload["model_run_evidence"][0]["status"] = "parsed_partial"
    payload["model_run_evidence"][0]["candidate_outcome"] = {"status": "active", "stage": "forcing"}
    payload["model_run_evidence"][1]["status"] = "failed"
    payload["model_run_evidence"][1]["candidate_outcome"] = {
        "status": "failed",
        "stage": "forcing",
        "reason": "forcing_task_failed",
    }
    return payload


def _scheduler_output_uri_unavailable_sibling_artifact(tmp_path: Path) -> Path:
    submitted_model = _scheduler_model("model_a", "basin_a")
    submitted_model["resource_profile"] = {
        **submitted_model["resource_profile"],
        "output_uri": "s3://nhms/runs/fcst_gfs_2026052106_model_a/output/",
    }
    orchestrator = FakeProductionOrchestrator(expose_object_store=False)
    scheduler = ProductionScheduler(
        _scheduler_config(tmp_path / "scheduler-workspace", now=_scheduler_dt("2026-05-21T12:00:00Z"), dry_run=False),
        registry=FakeRegistry([submitted_model, _scheduler_model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted_partial"
    assert result.evidence["status"] == "submitted_partial"
    assert result.evidence["counts"]["submitted_count"] == 1
    assert result.evidence["counts"]["partial_count"] == 1
    evidence_by_model = {item["model_id"]: item for item in result.evidence["model_run_evidence"]}
    assert evidence_by_model["model_a"]["submitted"] is True
    assert evidence_by_model["model_b"]["status"] == "blocked"
    assert evidence_by_model["model_b"]["submitted"] is False
    assert evidence_by_model["model_b"]["error_code"] == "OUTPUT_URI_UNAVAILABLE"
    assert result.artifact_path is not None
    return Path(result.artifact_path)


def _scheduler_failed_alias_sibling_artifact(tmp_path: Path, *, outcome_status: str) -> Path:
    sibling_reason = f"forcing_task_{outcome_status}"
    orchestrator = FakeProductionOrchestrator(
        candidate_outcomes=(
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "model_id": "model_a",
                "status": "active",
                "stage": "forcing",
            },
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_b",
                "model_id": "model_b",
                "status": outcome_status,
                "stage": "forcing",
                "reason": sibling_reason,
                "slurm_job_id": "slurm_forcing_1",
                "exit_code": 1,
            },
        ),
        result_status="parsed_partial",
    )
    scheduler = ProductionScheduler(
        _scheduler_config(
            tmp_path / f"scheduler-workspace-{outcome_status}",
            now=_scheduler_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
        ),
        registry=FakeRegistry([_scheduler_model("model_a", "basin_a"), _scheduler_model("model_b", "basin_b")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert result.status == "submitted_partial"
    assert result.evidence["status"] == "submitted_partial"
    assert result.evidence["counts"]["submitted_count"] == 2
    assert result.evidence["counts"]["failed_count"] == 1
    assert result.evidence["counts"]["partial_count"] == 1
    evidence_by_model = {item["model_id"]: item for item in result.evidence["model_run_evidence"]}
    assert evidence_by_model["model_b"]["status"] == outcome_status
    assert evidence_by_model["model_b"]["candidate_outcome"]["status"] == outcome_status
    assert result.artifact_path is not None
    return Path(result.artifact_path)


class NoSubmissionFailedOrchestrator(FakeProductionOrchestrator):
    def orchestrate_cycle(
        self,
        source: str,
        cycle_time: datetime,
        basins: list[dict[str, object]],
    ) -> PipelineResult:
        self.calls.append({"source": source, "cycle_time": cycle_time, "basins": basins})
        candidate_outcomes = tuple(
            {
                "candidate_id": basin["candidate_id"],
                "run_id": basin["run_id"],
                "model_id": basin["model_id"],
                "status": "failed",
                "stage": "forcing",
                "reason": "submission_failed",
                "accounting": {},
            }
            for basin in basins
        )
        return PipelineResult(
            run_id=f"cycle_{source.lower()}_{format_cycle_time(cycle_time)}",
            cycle_id=cycle_id_for(source, cycle_time),
            status="failed",
            stages=(
                StageRunResult(
                    stage="forcing",
                    job_type="produce_forcing_array",
                    pipeline_job_id="job_forcing",
                    slurm_job_id="",
                    status="submission_failed",
                    error_code="SBATCH_SUBMISSION_FAILED",
                    error_message="sbatch submission failed before a Slurm job id was assigned",
                ),
            ),
            candidate_outcomes=candidate_outcomes,
        )


def _scheduler_no_submission_failed_artifact(tmp_path: Path) -> Path:
    orchestrator = NoSubmissionFailedOrchestrator()
    scheduler = ProductionScheduler(
        _scheduler_config(
            tmp_path / "scheduler-workspace-no-submission-failed",
            now=_scheduler_dt("2026-05-21T12:00:00Z"),
            dry_run=False,
        ),
        registry=FakeRegistry([_scheduler_model("model_a", "basin_a")]),
        adapters={"gfs": FakeAdapter("gfs", [("2026-05-21T06:00:00Z", True)])},
        orchestrator_factory=lambda _source_id: orchestrator,
    )

    result = scheduler.run_once()

    assert len(orchestrator.calls) == 1
    assert result.status == "preflight_blocked"
    assert result.evidence["status"] == "preflight_blocked"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["counts"]["failed_count"] == 1
    assert result.evidence["counts"]["partial_count"] == 1
    model_run = result.evidence["model_run_evidence"][0]
    assert model_run["status"] == "failed"
    assert model_run["submitted"] is False
    assert model_run["execution_attempted"] is True
    assert model_run["stage_statuses"][0]["status"] == "submission_failed"
    assert result.artifact_path is not None
    return Path(result.artifact_path)


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda payload: payload["candidates"][0].pop("scenario_id"),
            "candidates_missing_scenario_id",
        ),
        (
            lambda payload: payload["skipped_candidates"][0].pop("scenario_id"),
            "skipped_candidates_missing_scenario_id",
        ),
        (
            lambda payload: (
                payload["blocked_candidates"].append(
                    {
                        "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                        "source_id": "gfs",
                        "cycle_time_utc": "2026-05-21T06:00:00Z",
                        "model_id": "model_b",
                        "reason": "operator_blocked",
                    }
                ),
                payload["counts"].__setitem__("candidate_count", 3),
                payload["counts"].__setitem__("blocked_candidate_count", 1),
            ),
            "blocked_candidates_missing_scenario_id",
        ),
        (
            lambda payload: payload["model_run_evidence"].append(
                {
                    "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                    "source_id": "gfs",
                    "cycle_time_utc": "2026-05-21T06:00:00Z",
                    "model_id": "model_b",
                    "run_id": "fcst_gfs_2026052106_model_b",
                    "forcing_version_id": "forc_gfs_2026052106_model_b",
                }
            ),
            "model_run_evidence_missing_scenario_id",
        ),
    ],
)
def test_scheduler_missing_scenario_id_blocks_evidence_and_matching_live_proof(
    tmp_path: Path,
    mutator: object,
    expected_error: str,
) -> None:
    payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")
    if "model_run_evidence" in expected_error:
        payload["counts"]["submitted_count"] = 1
    mutator(payload)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda payload: payload["candidates"][0].__setitem__("scenario_id", "forecast_ifs_deterministic"),
            "candidates_scenario_id_identity_mismatch",
        ),
        (
            lambda payload: payload["skipped_candidates"][0].__setitem__(
                "candidate_id",
                "IFS:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            ),
            "skipped_candidates_scenario_id_identity_mismatch",
        ),
        (
            lambda payload: (
                payload["blocked_candidates"].append(
                    {
                        "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
                        "source_id": "gfs",
                        "cycle_time_utc": "2026-05-21T06:00:00Z",
                        "model_id": "model_b",
                        "scenario_id": "sibling_scenario",
                        "reason": "operator_blocked",
                    }
                ),
                payload["counts"].__setitem__("candidate_count", 3),
                payload["counts"].__setitem__("blocked_candidate_count", 1),
            ),
            "blocked_candidates_scenario_id_identity_mismatch",
        ),
        (
            lambda payload: payload["model_run_evidence"][0].__setitem__(
                "candidate_id",
                "gfs:2026-05-21T06:00:00Z:model_a:sibling_scenario",
            ),
            "model_run_evidence_scenario_id_identity_mismatch",
        ),
    ],
)
def test_scheduler_mismatched_scenario_id_blocks_evidence_and_matching_live_proof(
    tmp_path: Path,
    mutator: object,
    expected_error: str,
) -> None:
    payload = _submitted_scheduler_payload()
    if "skipped_candidates" in expected_error:
        payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")
    mutator(payload)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("field", "wrong_value", "expected_error"),
    [
        ("run_id", "fcst_ifs_2026052106_model_a", "candidates_run_id_derivation_mismatch"),
        (
            "forcing_version_id",
            "forc_ifs_2026052106_model_a",
            "candidates_forcing_version_id_derivation_mismatch",
        ),
    ],
)
def test_scheduler_candidate_run_or_forcing_derivation_blocks_even_when_model_run_repeats_wrong_value(
    tmp_path: Path,
    field: str,
    wrong_value: str,
    expected_error: str,
) -> None:
    payload = _submitted_scheduler_payload()
    payload["candidates"][0][field] = wrong_value
    payload["model_run_evidence"][0][field] = wrong_value

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert expected_error in errors
    assert f"model_run_evidence_{field}_derivation_mismatch" in errors
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (
            lambda payload: payload["counts"].__setitem__("candidate_count", 3),
            "candidate_count_identity_cardinality_mismatch",
        ),
        (
            lambda payload: payload["counts"].__setitem__("submitted_count", 2),
            "submitted_count_model_run_evidence_mismatch",
        ),
        (
            lambda payload: payload["counts"].__setitem__("blocked_candidate_count", 1),
            "blocked_candidate_count_identity_cardinality_mismatch",
        ),
        (
            lambda payload: payload["counts"].__setitem__("skipped_candidate_count", 1),
            "skipped_candidate_count_identity_cardinality_mismatch",
        ),
        (
            lambda payload: payload["counts"].__setitem__("partial_count", 1),
            "partial_count_status_cardinality_mismatch",
        ),
        (
            lambda payload: payload["counts"].__setitem__("partial_count", 2),
            "partial_count_exceeds_model_run_evidence",
        ),
        (
            lambda payload: payload["counts"].__setitem__("failed_count", 1),
            "failed_count_status_cardinality_mismatch",
        ),
        (
            lambda payload: payload["counts"].__setitem__("failed_count", 2),
            "failed_count_exceeds_model_run_evidence",
        ),
    ],
)
def test_scheduler_count_list_cardinality_mismatch_blocks_evidence_and_matching_live_proof(
    tmp_path: Path,
    mutator: object,
    expected_error: str,
) -> None:
    payload = _submitted_scheduler_payload()
    mutator(payload)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda row: row.__setitem__("candidate_outcome", {"status": "failed"}),
        lambda row: (
            row.__setitem__("status", "failed"),
            row.__setitem__("candidate_outcome", {"status": "submitted"}),
        ),
        lambda row: row.__setitem__("state", "failed"),
        lambda row: row.__setitem__("result", "failed"),
        lambda row: (
            row.__setitem__("status", "partial"),
            row.__setitem__("candidate_outcome", {"status": "submitted"}),
        ),
    ],
)
def test_scheduler_mixed_live_and_failed_or_partial_aliases_block_live_binding(
    tmp_path: Path,
    mutator: object,
) -> None:
    payload = _submitted_scheduler_payload()
    mutator(payload["model_run_evidence"][0])
    payload["counts"]["failed_count"] = 1
    payload["counts"]["partial_count"] = 1

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "live_status_failed_count_nonzero" in errors
    assert "live_status_partial_count_nonzero" in errors
    assert "live_status_model_run_blocked_outcome" in errors
    assert "submitted_status_model_run_status_mismatch" in errors
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "nested_status",
    [
        {"candidate_outcome": {"status": "submitted", "state": "failed"}},
        {"candidate_outcome": {"status": "submitted", "result": "failed"}},
        {"candidate_outcome": {"status": "submitted", "outcome": "partial"}},
        {"outcome": {"status": "submitted", "state": "failed"}},
        {"result": {"status": "submitted", "state": "failed"}},
    ],
)
def test_scheduler_hidden_nested_failed_or_partial_aliases_block_live_binding(
    tmp_path: Path,
    nested_status: dict[str, object],
) -> None:
    payload = _submitted_scheduler_payload()
    payload["model_run_evidence"][0] |= nested_status

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "live_status_model_run_blocked_outcome" in errors
    assert "submitted_status_model_run_status_mismatch" in errors
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "nested_status",
    [
        {"candidate_outcome": [{"status": "submitted"}, {"state": "failed"}]},
        {"outcome": [{"status": "submitted"}, {"result": "failed"}]},
        {"result": [{"status": "submitted"}, {"outcome": "partial"}]},
    ],
)
def test_scheduler_hidden_sequence_failed_or_partial_aliases_block_live_binding(
    tmp_path: Path,
    nested_status: dict[str, object],
) -> None:
    payload = _submitted_scheduler_payload()
    payload["model_run_evidence"][0] |= nested_status

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "live_status_model_run_blocked_outcome" in errors
    assert "submitted_status_model_run_status_mismatch" in errors
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("count_field", "expected_error"),
    [
        ("failed_count", "live_status_failed_count_nonzero"),
        ("partial_count", "live_status_partial_count_nonzero"),
    ],
)
def test_scheduler_live_status_blocks_nonzero_failed_or_partial_count(
    tmp_path: Path,
    count_field: str,
    expected_error: str,
) -> None:
    payload = _submitted_scheduler_payload()
    payload["counts"][count_field] = 1

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("row_status", "mutator", "expected_error"),
    [
        ("failed", lambda payload: payload["counts"].pop("failed_count"), "missing_failed_count"),
        (
            "failed",
            lambda payload: payload["counts"].__setitem__("failed_count", 0),
            "failed_count_status_cardinality_mismatch",
        ),
        (
            "submission_failed",
            lambda payload: payload["counts"].__setitem__("failed_count", 0),
            "failed_count_status_cardinality_mismatch",
        ),
        (
            "permanently_failed",
            lambda payload: payload["counts"].__setitem__("failed_count", 0),
            "failed_count_status_cardinality_mismatch",
        ),
    ],
)
def test_scheduler_failed_model_run_status_requires_matching_failed_count(
    tmp_path: Path,
    row_status: str,
    mutator: object,
    expected_error: str,
) -> None:
    payload = _failed_scheduler_payload(status=row_status)
    mutator(payload)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize("row_status", ["submission_failed", "permanently_failed"])
def test_scheduler_failed_count_accepts_failed_status_aliases(tmp_path: Path, row_status: str) -> None:
    payload = _failed_scheduler_payload(status=row_status)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "failed_count_status_cardinality_mismatch" not in errors
    assert errors == []
    assert scheduler_item["details"]["failed_count"] == 1
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (lambda payload: payload["counts"].pop("partial_count"), "missing_partial_count"),
        (
            lambda payload: payload["counts"].__setitem__("partial_count", 0),
            "partial_count_status_cardinality_mismatch",
        ),
    ],
)
def test_scheduler_partial_model_run_status_requires_matching_partial_count(
    tmp_path: Path,
    mutator: object,
    expected_error: str,
) -> None:
    payload = _submitted_scheduler_payload()
    payload["status"] = "submitted_partial"
    payload["model_run_evidence"][0]["status"] = "partial"
    payload["model_run_evidence"][0]["candidate_outcome"] = {"status": "partial"}
    mutator(payload)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_model_run_evidence_absent_candidate_id_blocks_evidence_and_live_binding(tmp_path: Path) -> None:
    payload = _submitted_scheduler_payload()
    payload["model_run_evidence"][0] |= {
        "candidate_id": "gfs:2026-05-21T06:00:00Z:model_b:forecast_gfs_deterministic",
        "model_id": "model_b",
        "run_id": "fcst_gfs_2026052106_model_b",
        "forcing_version_id": "forc_gfs_2026052106_model_b",
    }

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "model_run_evidence_candidate_not_selected" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_model_run_evidence_skipped_candidate_id_blocks_evidence_and_live_binding(tmp_path: Path) -> None:
    payload = _scheduler_evidence_payload(status="submitted", execution_mode="production_orchestration")
    skipped = payload["skipped_candidates"][0]
    payload["counts"]["submitted_count"] = 1
    payload["model_run_evidence"] = [
        {
            "candidate_id": skipped["candidate_id"],
            "source_id": skipped["source_id"],
            "cycle_time_utc": skipped["cycle_time_utc"],
            "model_id": skipped["model_id"],
            "scenario_id": skipped["scenario_id"],
            "run_id": "fcst_ifs_2026052106_model_a",
            "forcing_version_id": "forc_ifs_2026052106_model_a",
            "status": "submitted",
            "submitted": True,
        }
    ]

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "model_run_evidence_candidate_not_selected" in scheduler_item["details"]["acceptance_errors"]
    assert "model_run_evidence_candidate_skipped" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_model_run_evidence_blocked_candidate_id_blocks_evidence_and_live_binding(tmp_path: Path) -> None:
    payload = _submitted_scheduler_payload()
    blocked = {
        **payload["candidates"][0],
        "reason": "operator_blocked",
    }
    payload["blocked_candidates"] = [blocked]
    payload["candidates"] = []
    payload["counts"]["blocked_candidate_count"] = 1

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "model_run_evidence_candidate_not_selected" in scheduler_item["details"]["acceptance_errors"]
    assert "model_run_evidence_candidate_blocked" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload["candidates"].append(_submitted_scheduler_payload()["candidates"][0]),
        lambda payload: payload["model_run_evidence"].append(_submitted_scheduler_payload()["model_run_evidence"][0]),
    ],
)
def test_scheduler_candidate_count_zero_with_candidate_or_model_run_rows_blocks_evidence(
    tmp_path: Path,
    mutator: object,
) -> None:
    payload = _scheduler_evidence_payload(status="planned", execution_mode="dry_run")
    payload["counts"]["candidate_count"] = 0
    payload["counts"]["skipped_candidate_count"] = 0
    payload["candidates"] = []
    payload["skipped_candidates"] = []
    payload["model_run_evidence"] = []
    mutator(payload)
    if payload["model_run_evidence"]:
        payload["counts"]["submitted_count"] = 1

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "candidate_count_identity_cardinality_mismatch" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_partial_count_accepts_matching_terminal_model_run_row(tmp_path: Path) -> None:
    payload = _partial_scheduler_payload()

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["details"]["acceptance_errors"] == []
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_submitted_partial_count_accepts_failed_submitted_sibling(tmp_path: Path) -> None:
    payload = _submitted_partial_scheduler_payload_with_failed_sibling()

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "partial_count_status_cardinality_mismatch" not in errors
    assert "failed_count_status_cardinality_mismatch" not in errors
    assert errors == []
    assert scheduler_item["details"]["submitted_count"] == 2
    assert scheduler_item["details"]["failed_count"] == 1
    assert scheduler_item["details"]["partial_count"] == 1
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_submitted_partial_count_accepts_output_uri_unavailable_blocked_sibling(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = _scheduler_output_uri_unavailable_sibling_artifact(tmp_path)
    payload = json.loads(scheduler_path.read_text(encoding="utf-8"))
    receipt = _scheduler_proof_bound_to_evidence(
        scheduler_path,
        producer_artifact_ref=f"scheduler:{scheduler_path.name}",
        producer_run_id=str(payload["pass_id"]),
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["required_for_final"] is False
    assert "partial_count_status_cardinality_mismatch" not in errors
    assert errors == []
    assert scheduler_item["details"]["submitted_count"] == 1
    assert scheduler_item["details"]["partial_count"] == 1
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize("outcome_status", ["submission_failed", "permanently_failed"])
def test_scheduler_submitted_partial_count_accepts_produced_failed_alias_sibling(
    tmp_path: Path,
    outcome_status: str,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = _scheduler_failed_alias_sibling_artifact(tmp_path, outcome_status=outcome_status)
    payload = json.loads(scheduler_path.read_text(encoding="utf-8"))
    receipt = _scheduler_proof_bound_to_evidence(
        scheduler_path,
        producer_artifact_ref=f"scheduler:{scheduler_path.name}",
        producer_run_id=str(payload["pass_id"]),
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["required_for_final"] is False
    assert "failed_count_status_cardinality_mismatch" not in errors
    assert "partial_count_status_cardinality_mismatch" not in errors
    assert errors == []
    assert scheduler_item["details"]["submitted_count"] == 2
    assert scheduler_item["details"]["failed_count"] == 1
    assert scheduler_item["details"]["partial_count"] == 1
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_no_submission_failed_artifact_counts_as_stable_blocked_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = _scheduler_no_submission_failed_artifact(tmp_path)
    payload = json.loads(scheduler_path.read_text(encoding="utf-8"))
    receipt = _scheduler_proof_bound_to_evidence(
        scheduler_path,
        producer_artifact_ref=f"scheduler:{scheduler_path.name}",
        producer_run_id=str(payload["pass_id"]),
    )

    assert payload["counts"]["submitted_count"] == 0
    assert payload["counts"]["failed_count"] == 1
    assert payload["counts"]["partial_count"] == 1

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_file=scheduler_path,
            scheduler_proof=receipt,
        )
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["required_for_final"] is False
    assert "failed_count_exceeds_model_run_evidence" not in errors
    assert "failed_count_status_cardinality_mismatch" not in errors
    assert "partial_count_exceeds_model_run_evidence" not in errors
    assert "partial_count_status_cardinality_mismatch" not in errors
    assert errors == []
    assert scheduler_item["details"]["submitted_count"] == 0
    assert scheduler_item["details"]["failed_count"] == 1
    assert scheduler_item["details"]["partial_count"] == 1
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize("status", ["preflight_blocked", "blocked"])
def test_scheduler_blocked_model_run_row_with_zero_submitted_count_remains_stable_blocked_evidence(
    tmp_path: Path,
    status: str,
) -> None:
    payload = _submitted_scheduler_payload()
    payload["status"] = status
    payload["counts"]["submitted_count"] = 0
    payload["model_run_evidence"][0]["status"] = "preflight_blocked"
    payload["model_run_evidence"][0]["submitted"] = False
    payload["model_run_evidence"][0]["execution_attempted"] = False

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["details"]["acceptance_errors"] == []
    assert scheduler_item["details"]["submitted_count"] == 0
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_root_file_limit_blocks_before_per_file_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler"
    scheduler_root.mkdir()
    for index in range(MAX_SCHEDULER_EVIDENCE_FILES + 1):
        (scheduler_root / f"evidence_{index:02d}.json").write_text(
            json.dumps(_scheduler_evidence_payload(pass_id=f"scheduler_20260521120000_{index:02d}")),
            encoding="utf-8",
        )

    def fail_if_file_validation_runs(path: Path) -> Path:
        raise AssertionError(f"unexpected per-file scheduler evidence validation: {path}")

    monkeypatch.setattr(
        "services.production_closure.readiness_validation._safe_scheduler_evidence_file",
        fail_if_file_validation_runs,
    )

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_root=scheduler_root)
    )

    scheduler_items = [item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence"]
    assert len(scheduler_items) == 1
    assert scheduler_items[0]["status"] == "blocked"
    assert scheduler_items[0]["details"]["error_code"] == "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_FILE_LIMIT"
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert live_item["status"] == "release_blocked"
    assert live_item["live_proof_accepted"] is False
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "extra_payload",
    [
        {"node_overflow": [{"i": index} for index in range(1300)]},
        {
            "depth_overflow": {
                "a": {
                    "b": {
                        "c": {
                            "d": {
                                "e": {
                                    "f": {
                                        "g": {
                                            "h": {
                                                "i": {
                                                    "j": {
                                                        "k": {
                                                            "l": {
                                                                "m": {
                                                                    "n": {
                                                                        "o": {
                                                                            "p": {
                                                                                "q": {
                                                                                    "r": "too deep",
                                                                                }
                                                                            }
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
    ],
)
def test_scheduler_json_traversal_over_limits_bounds_details_without_blocking_evidence(
    tmp_path: Path,
    extra_payload: dict[str, object],
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    payload = _scheduler_evidence_payload(**extra_payload)
    _write_scheduler_payload(scheduler_path, payload)

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    rendered_details = json.dumps(item["details"], sort_keys=True)
    assert item["status"] == "passed"
    assert item["details"]["acceptance_errors"] == []
    assert "[truncated:max-" in rendered_details
    assert "scheduler_evidence_json_" not in rendered_details
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_rich_two_model_submitted_payload_validates_from_raw_payload_with_bounded_details(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    payload = _two_model_submitted_scheduler_payload()
    payload["rich_details"] = [{"index": index, "safe_value": f"value-{index}"} for index in range(1300)]
    payload["local_path"] = "/tmp/private-scheduler-evidence/source.json"
    payload["signed_uri"] = "s3://nhms/runs/log.out?token=secret"
    _write_scheduler_payload(scheduler_path, payload)
    assert scheduler_path.stat().st_size < 256 * 1024

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    rendered_details = json.dumps(scheduler_item["details"], sort_keys=True)
    assert scheduler_item["status"] == "passed"
    assert scheduler_item["details"]["acceptance_errors"] == []
    for unexpected_error in (
        "schema_mismatch",
        "status_not_allowed",
        "submitted_count_model_run_evidence_mismatch",
        "scheduler_evidence_json_node_limit_exceeded",
    ):
        assert unexpected_error not in rendered_details
    assert scheduler_item["details"]["candidate_count"] == 2
    assert scheduler_item["details"]["submitted_count"] == 2
    assert "[truncated:max-nodes]" in rendered_details
    assert "private-scheduler-evidence" not in rendered_details
    assert "token=secret" not in rendered_details
    assert live_item["status"] == "release_blocked"
    assert live_item["live_proof_accepted"] is False
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
