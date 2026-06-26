from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from services.orchestrator.chain import PipelineResult, StageRunResult
from services.orchestrator.scheduler import ProductionScheduler
from services.production_closure import (
    readiness_dependency_summaries,
    readiness_item_contracts,
    readiness_live_proofs,
    readiness_scheduler_evidence,
    readiness_shared_artifacts,
    readiness_validation,
    slurm_validation,
)
from services.production_closure.readiness_validation import (
    ALLOWED_STATUS_EXECUTION_MODES,
    EXECUTED_MODES,
    EXECUTION_MODE_VALUES,
    MAX_SCHEDULER_EVIDENCE_FILES,
    STATUS_VALUES,
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
DEPENDENCY_PROOF_PARAMS = (
    pytest.param("slurm", id="slurm"),
    pytest.param("object_store", id="object-store"),
    pytest.param("source", id="source"),
    pytest.param("e2e", id="end-to-end"),
    pytest.param("mvt", id="mvt"),
)
DEPENDENCY_CONTRACTS = {
    "slurm": (147, "nhms.production_closure.slurm.v1"),
    "object_store": (148, "nhms.production_closure.object_store.v1"),
    "source": (149, "nhms.production_closure.met.v1"),
    "e2e": (150, "nhms.production_closure.e2e.v1"),
    "mvt": (151, "nhms.production_closure.scale.v1"),
}
DEPENDENCY_EVIDENCE_ROOT_OPTIONS = {
    "slurm": "--slurm-evidence-root",
    "object_store": "--object-store-evidence-root",
    "source": "--source-evidence-root",
    "e2e": "--e2e-evidence-root",
    "mvt": "--mvt-evidence-root",
}
EXPECTED_ALLOWED_STATUS_EXECUTION_MODES = {
    "passed": frozenset(
        {
            "deterministic",
            "policy_simulated",
            "backend_route_executed",
            "dry_run_sink",
            "simulated_drill",
            "live_proof",
        }
    ),
    "failed": frozenset(
        {
            "deterministic",
            "policy_simulated",
            "backend_route_executed",
            "dry_run_sink",
            "simulated_drill",
            "live_proof",
        }
    ),
    "blocked": frozenset({"not_executed"}),
    "not_executed": frozenset({"not_executed"}),
    "release_blocked": frozenset(
        {
            "not_executed",
            "policy_simulated",
            "dry_run_sink",
            "simulated_drill",
            "live_proof",
        }
    ),
}
EXPECTED_EXECUTION_MODE_VALUES = frozenset().union(*EXPECTED_ALLOWED_STATUS_EXECUTION_MODES.values())
EXPECTED_REQUIRED_READINESS_ITEM_FIELDS = (
    "item_id",
    "surface",
    "required_for_final",
    "live_proof_accepted",
    "artifact_refs",
    "residual_risk",
    "removal_criteria",
    "exclusions",
    "owner",
    "action",
)


class ReadyCanonicalReadinessProvider:
    def canonical_readiness(self, **kwargs: object) -> dict[str, object]:
        return {
            "status": "canonical_ready",
            "ready": True,
            "reason": None,
            "source_id": kwargs["source_id"],
            "cycle_time": kwargs["cycle_time"],
            "forecast_hours": list(kwargs["forecast_hours"]),
            "policy_identity": dict(kwargs["policy_identity"]),
            "source_object_identity": dict(kwargs["source_object_identity"]),
            "canonical_product_id": kwargs["canonical_product_id"],
            "model_id": kwargs["model_id"],
            "basin_id": kwargs["basin_id"],
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
        "item_id": "unit-readiness-item",
        "surface": "unit",
        "status": status,
        "execution_mode": execution_mode,
        "required_for_final": False,
        "live_proof_accepted": False,
        "artifact_refs": [],
        "residual_risk": "unit residual risk",
        "removal_criteria": "unit removal criteria",
        "exclusions": [],
        "owner": "unit-owner",
        "action": "unit-action",
    }


def _assert_contract_error(
    validator: object,
    item: dict[str, object],
    expected_error_code: str,
) -> None:
    with pytest.raises(ProductionReadinessValidationError) as error:
        validator(item)
    assert error.value.error_code == expected_error_code


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
    payload = _dependency_summary_payload(proof_key, run_id=run_id)
    root.mkdir(parents=True, exist_ok=True)
    summary_path = root / "summary.json"
    summary_path.write_text(json.dumps(payload), encoding="utf-8")
    return "sha256:" + hashlib.sha256(summary_path.read_bytes()).hexdigest()


def _dependency_summary_payload(proof_key: str, *, run_id: str | None = None) -> dict[str, object]:
    issue, schema = DEPENDENCY_CONTRACTS[proof_key]
    return {
        "schema": schema,
        "issue": issue,
        "run_id": run_id or f"{proof_key}-live-run",
        "status": "ready",
        "execution_mode": "deterministic_fixture",
        "final_production_readiness_claimed": False,
    }


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _write_symlink_summary(summary_root: Path, *, leaf: bool) -> None:
    target_root = summary_root.parent / "real-summary"
    target_root.mkdir(parents=True, exist_ok=True)
    (target_root / "summary.json").write_text(json.dumps(_dependency_summary_payload("slurm")), encoding="utf-8")
    if leaf:
        summary_root.mkdir(parents=True, exist_ok=True)
        (summary_root / "summary.json").symlink_to(target_root / "summary.json")
        return
    summary_root.parent.mkdir(parents=True, exist_ok=True)
    summary_root.symlink_to(target_root, target_is_directory=True)


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
            "slurm_status_sync_called": False,
            "slurm_cancellation_called": False,
            "shud_runtime_called": False,
            "hydro_result_table_writes": False,
            "met_result_table_writes": False,
            "pipeline_status_writes": False,
            "pipeline_event_writes": False,
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


def _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
    root: Path,
    expected: str,
    *,
    expected_in: str = "acceptance_errors",
) -> tuple[dict[str, object], dict[str, object]]:
    scheduler_item = next(item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence")
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["execution_mode"] == "not_executed"
    assert scheduler_item["live_proof_accepted"] is False
    if expected_in == "acceptance_errors":
        assert expected in scheduler_item["details"]["acceptance_errors"]
    elif expected_in == "error_code":
        assert scheduler_item["details"]["error_code"] == expected
    else:
        raise AssertionError(f"unknown scheduler evidence expectation type: {expected_in}")
    assert live_item["status"] == "release_blocked"
    assert live_item["live_proof_accepted"] is False
    acceptance_errors = live_item.get("details", {}).get("acceptance_errors")
    if isinstance(acceptance_errors, dict) and "errors" in acceptance_errors:
        assert "missing_scheduler_evidence_binding" in acceptance_errors["errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False
    return scheduler_item, live_item


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


def test_status_execution_mode_truth_table_facade_reexports_owner_contract_objects() -> None:
    assert STATUS_VALUES is readiness_item_contracts.STATUS_VALUES
    assert EXECUTION_MODE_VALUES is readiness_item_contracts.EXECUTION_MODE_VALUES
    assert EXECUTED_MODES is readiness_item_contracts.EXECUTED_MODES
    assert ALLOWED_STATUS_EXECUTION_MODES is readiness_item_contracts.ALLOWED_STATUS_EXECUTION_MODES
    assert ProductionReadinessValidationError is readiness_item_contracts.ProductionReadinessValidationError
    assert validate_readiness_item is readiness_item_contracts.validate_readiness_item
    assert readiness_validation.STATUS_VALUES is readiness_item_contracts.STATUS_VALUES
    assert readiness_validation.EXECUTION_MODE_VALUES is readiness_item_contracts.EXECUTION_MODE_VALUES
    assert readiness_validation.EXECUTED_MODES is readiness_item_contracts.EXECUTED_MODES
    assert (
        readiness_validation.ALLOWED_STATUS_EXECUTION_MODES
        is readiness_item_contracts.ALLOWED_STATUS_EXECUTION_MODES
    )
    assert (
        readiness_validation.ProductionReadinessValidationError
        is readiness_item_contracts.ProductionReadinessValidationError
    )
    assert readiness_validation.validate_readiness_item is readiness_item_contracts.validate_readiness_item


def test_shared_artifact_facade_reexports_owner_helpers_and_outputs_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper_names = (
        "EvidenceWriter",
        "BoundedPayloadResult",
        "_preflight_payload",
        "_environment_payload",
        "_load_proof",
        "_receipt_artifact",
        "_receipt_details",
        "_receipt_validation_payload",
        "_path_for_evidence",
        "_redact_paths",
        "_bounded_payload",
        "_bounded_redacted_payload",
    )
    for name in helper_names:
        assert getattr(readiness_validation, name) is getattr(readiness_shared_artifacts, name)

    for name in (
        "MAX_EVIDENCE_PAYLOAD_BYTES",
        "MAX_JSON_DEPTH",
        "MAX_JSON_NODES",
        "MAX_STRING_LENGTH",
        "MAX_RECEIPT_BYTES",
        "MAX_RECEIPT_PREVIEW_BYTES",
        "LIVE_PROOF_SCHEMA",
    ):
        assert getattr(readiness_validation, name) == getattr(readiness_shared_artifacts, name)
    assert readiness_validation.PATH_TOKEN_RE is readiness_shared_artifacts.PATH_TOKEN_RE
    assert readiness_validation.DEPENDENCY_ROOT_ENV is readiness_shared_artifacts.DEPENDENCY_ROOT_ENV
    assert readiness_validation.PROOF_ENV is readiness_shared_artifacts.PROOF_ENV
    assert readiness_validation.PROOF_FILE_ENV is readiness_shared_artifacts.PROOF_FILE_ENV
    assert (
        readiness_validation.SCHEDULER_EVIDENCE_ROOT_ENV
        is readiness_shared_artifacts.SCHEDULER_EVIDENCE_ROOT_ENV
    )
    assert (
        readiness_validation.SCHEDULER_EVIDENCE_FILE_ENV
        is readiness_shared_artifacts.SCHEDULER_EVIDENCE_FILE_ENV
    )

    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        slurm_evidence_root=tmp_path / "artifacts" / "slurm",
    )
    receipts = {"auth": {"status": "missing"}, "alert": {"status": "parsed"}}
    assert readiness_validation._preflight_payload(config, receipts) == readiness_shared_artifacts._preflight_payload(
        config, receipts
    )

    monkeypatch.setenv("AUTH_TOKEN", "token=secret")
    monkeypatch.setattr(readiness_shared_artifacts, "_now", lambda: "2026-01-01T00:00:00Z")
    assert readiness_validation._environment_payload(config) == readiness_shared_artifacts._environment_payload(config)


def test_live_proof_loader_owner_facade_parity_and_raw_payload_boundary(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    raw_path = "/tmp/private-live-proof/receipt.json"
    payload = json.loads(_bound_proof("alert"))
    payload["local_path"] = raw_path
    payload["signed_uri"] = "https://user:pass@alerts.example.invalid/hook?token=secret"
    proof = json.dumps(payload)

    owner_receipt = readiness_shared_artifacts._load_proof("alert", proof, None, config=config)
    facade_receipt = readiness_validation._load_proof("alert", proof, None, config=config)

    assert owner_receipt == facade_receipt
    assert owner_receipt["status"] == "parsed"
    validation_payload = readiness_shared_artifacts._receipt_validation_payload(owner_receipt)
    assert validation_payload["local_path"] == raw_path
    assert validation_payload["signed_uri"].endswith("token=secret")

    public_details = readiness_shared_artifacts._receipt_details(owner_receipt, config=config)
    rendered_details = json.dumps(public_details, sort_keys=True)
    assert public_details == readiness_validation._receipt_details(owner_receipt, config=config)
    assert "raw_payload" not in rendered_details
    assert raw_path not in rendered_details
    assert "token=secret" not in rendered_details
    assert "user:pass@" not in rendered_details
    assert "[redacted" in rendered_details

    receipts = {
        surface: {"surface": surface, "status": "missing", "source": "not_configured"}
        for surface in readiness_shared_artifacts.PROOF_ENV
    }
    receipts["alert"] = owner_receipt
    artifact = readiness_shared_artifacts._receipt_artifact(config, receipts)
    rendered_artifact = json.dumps(artifact, sort_keys=True)
    assert artifact == readiness_validation._receipt_artifact(config, receipts)
    assert artifact["schema"] == "nhms.production_readiness.live_proof_receipts.v1"
    assert artifact["run_id"] == "m19"
    assert set(artifact["receipts"]) == set(readiness_shared_artifacts.PROOF_ENV)
    assert artifact["redaction"] == {
        "secrets_redacted": True,
        "local_paths_redacted": True,
        "payload_depth_bounded": True,
        "payload_size_bounded": True,
    }
    assert "raw_payload" not in rendered_artifact
    assert raw_path not in rendered_artifact
    assert "token=secret" not in rendered_artifact


def test_live_receipt_validator_owner_facade_aliases_and_outputs_match(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")

    assert readiness_validation.PROOF_CONTRACTS is readiness_live_proofs.PROOF_CONTRACTS
    assert readiness_validation.REQUIRED_AUTH_ACTIONS is readiness_live_proofs.REQUIRED_AUTH_ACTIONS
    assert readiness_validation.EXPECTED_TARGET_ENVIRONMENT == readiness_live_proofs.EXPECTED_TARGET_ENVIRONMENT
    assert readiness_validation.PROOF_SPECIFIC_KEYS is readiness_live_proofs.PROOF_SPECIFIC_KEYS
    for name in (
        "_is_live_proof_mode",
        "_has_artifact_or_evidence_refs",
        "_non_empty_string",
        "_has_meaningful_value",
        "_has_meaningful_ref",
        "_target_environment_name",
        "_string_set",
        "_provider_metadata_is_meaningful",
        "_role_mapping_is_meaningful",
        "_alert_sink_metadata_is_meaningful",
        "_alert_delivery_metadata_is_meaningful",
        "_rollback_command_metadata_is_meaningful",
        "_rollback_result_is_meaningful",
        "_target_env_config_metadata_is_meaningful",
        "_first_meaningful_mapping",
        "_has_any_key_value",
        "_value_from",
    ):
        assert getattr(readiness_validation, name) is getattr(readiness_live_proofs, name)

    auth_receipt = readiness_shared_artifacts._load_proof(
        "auth",
        _auth_proof(allowed=_all_auth_actions(), denied=_all_auth_actions()),
        None,
        config=config,
    )
    assert readiness_validation._auth_live_item(config, auth_receipt) == readiness_live_proofs._auth_live_item(
        config, auth_receipt
    )
    auth_payload = readiness_shared_artifacts._receipt_validation_payload(auth_receipt)
    assert readiness_validation._common_live_receipt_errors(auth_payload, proof_key="auth", config=config) == (
        readiness_live_proofs._common_live_receipt_errors(auth_payload, proof_key="auth", config=config)
    )

    surface_kwargs = {
        "alert": {
            "item_id": "live-alert-sink",
            "surface": "live_alert_sink_delivery",
            "missing_risk": "Live alert sink delivery has not been proven.",
            "removal": "Provide an accepted alert sink receipt.",
        },
        "rollback": {
            "item_id": "live-rollback-drill",
            "surface": "live_rollback_execution",
            "missing_risk": "Live rollback execution has not been proven.",
            "removal": "Provide an accepted rollback drill receipt.",
        },
        "target_env": {
            "item_id": "live-target-environment-config",
            "surface": "target_environment_config_proof",
            "missing_risk": "Real target-environment configuration receipt has not been accepted.",
            "removal": "Provide an accepted target-environment configuration receipt.",
        },
    }
    for proof_key, kwargs in surface_kwargs.items():
        receipt = readiness_shared_artifacts._load_proof(proof_key, _bound_proof(proof_key), None, config=config)
        assert readiness_validation._surface_live_item(
            config,
            receipt,
            proof_key=proof_key,
            dependency_bindings={},
            **kwargs,
        ) == readiness_live_proofs._surface_live_item(
            config,
            receipt,
            proof_key=proof_key,
            dependency_bindings={},
            **kwargs,
        )
        payload = readiness_shared_artifacts._receipt_validation_payload(receipt)
        assert readiness_validation._surface_live_receipt_errors(
            payload,
            proof_key=proof_key,
            config=config,
            dependency_bindings={},
        ) == readiness_live_proofs._surface_live_receipt_errors(
            payload,
            proof_key=proof_key,
            config=config,
            dependency_bindings={},
        )


def test_live_receipt_validator_facade_honors_old_monkeypatch_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    auth_receipt = readiness_shared_artifacts._load_proof(
        "auth",
        _auth_proof(allowed=_all_auth_actions(), denied=_all_auth_actions()),
        None,
        config=config,
    )
    alert_receipt = readiness_shared_artifacts._load_proof("alert", _bound_proof("alert"), None, config=config)

    monkeypatch.setattr(readiness_validation, "_provider_metadata_is_meaningful", lambda _payload: False)
    auth_item = readiness_validation._auth_live_item(config, auth_receipt)

    assert auth_item["status"] == "release_blocked"
    assert "missing_provider_metadata" in auth_item["details"]["acceptance_errors"]["errors"]

    monkeypatch.setattr(
        readiness_validation,
        "_common_live_receipt_errors",
        lambda _payload, *, proof_key, config: ["patched_common_live_error"],
    )
    alert_item = readiness_validation._surface_live_item(
        config,
        alert_receipt,
        proof_key="alert",
        dependency_bindings={},
        item_id="live-alert-sink",
        surface="live_alert_sink_delivery",
        missing_risk="Live alert sink delivery has not been proven.",
        removal="Provide an accepted alert sink receipt.",
    )

    assert alert_item["status"] == "release_blocked"
    assert "patched_common_live_error" in alert_item["details"]["acceptance_errors"]["errors"]


def test_live_receipt_items_delegate_only_proof_specific_builders_to_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    original_auth_live_item = readiness_live_proofs._auth_live_item
    original_surface_live_item = readiness_live_proofs._surface_live_item
    delegated: list[str] = []

    def recording_auth_live_item(*args: object, **kwargs: object) -> dict[str, object]:
        delegated.append("auth")
        return original_auth_live_item(*args, **kwargs)

    def recording_surface_live_item(*args: object, **kwargs: object) -> dict[str, object]:
        proof_key = str(kwargs["proof_key"])
        delegated.append(proof_key)
        assert proof_key in {"alert", "rollback", "target_env"}
        return original_surface_live_item(*args, **kwargs)

    monkeypatch.setattr(readiness_live_proofs, "_auth_live_item", recording_auth_live_item)
    monkeypatch.setattr(readiness_live_proofs, "_surface_live_item", recording_surface_live_item)

    proof_json = {
        "auth": _auth_proof(allowed=_all_auth_actions(), denied=_all_auth_actions()),
        "alert": _bound_proof("alert"),
        "rollback": _bound_proof("rollback"),
        "slurm": _bound_proof("slurm"),
        "object_store": _bound_proof("object_store"),
        "source": _bound_proof("source"),
        "e2e": _bound_proof("e2e"),
        "mvt": _bound_proof("mvt"),
        "target_env": _bound_proof("target_env"),
    }
    receipts = {
        proof_key: readiness_shared_artifacts._load_proof(
            proof_key,
            proof_json.get(proof_key),
            None,
            config=config,
        )
        for proof_key in readiness_shared_artifacts.PROOF_ENV
    }

    items = readiness_validation._live_proof_items(config, receipts, {}, ())

    assert {item["surface"] for item in items} >= {
        "live_backend_auth",
        "live_alert_sink_delivery",
        "live_rollback_execution",
        "live_slurm_dependency_proof",
        "live_object_store_dependency_proof",
        "live_source_weather_dependency_proof",
        "live_e2e_dependency_proof",
        "live_mvt_performance_proof",
        "target_environment_config_proof",
    }
    assert delegated == ["auth", "alert", "rollback", "target_env"]


def test_live_proof_loader_ambiguity_missing_and_preflight_status(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    proof_file = tmp_path / "alert-proof.json"
    proof_file.write_text(_bound_proof("alert"), encoding="utf-8")

    ambiguous = readiness_shared_artifacts._load_proof("alert", _bound_proof("alert"), proof_file, config=config)
    missing = readiness_shared_artifacts._load_proof("auth", None, None, config=config)

    assert ambiguous["status"] == "invalid"
    assert ambiguous["error_code"] == "PRODUCTION_READINESS_PROOF_AMBIGUOUS"
    assert missing["status"] == "missing"
    preflight = readiness_shared_artifacts._preflight_payload(config, {"auth": missing, "alert": ambiguous})
    assert preflight["live_proof_configured"] == {"auth": False, "alert": True}


def test_live_proof_loader_rejects_unsafe_file_with_redacted_path(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    private_dir = tmp_path / "private"
    private_dir.mkdir()
    target = private_dir / "auth-proof.json"
    target.write_text(_auth_proof(allowed=_all_auth_actions(), denied=_all_auth_actions()), encoding="utf-8")
    proof_file = private_dir / "symlink-proof.json"
    proof_file.symlink_to(target)

    receipt = readiness_shared_artifacts._load_proof("auth", None, proof_file, config=config)
    details = readiness_shared_artifacts._receipt_details(receipt, config=config)
    rendered = json.dumps(details, sort_keys=True)

    assert receipt["status"] == "invalid"
    assert receipt["error_code"] == "PRODUCTION_READINESS_PROOF_FILE_INVALID"
    assert receipt["path"] == "[redacted-path]"
    assert str(proof_file) not in rendered
    assert "private" not in rendered
    assert "[redacted-path]" in rendered


def test_live_proof_loader_rejects_non_regular_file_with_redacted_reason(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    proof_file = tmp_path / "private" / "auth-proof-dir"
    proof_file.mkdir(parents=True)

    receipt = readiness_shared_artifacts._load_proof("auth", None, proof_file, config=config)
    details = readiness_shared_artifacts._receipt_details(receipt, config=config)
    rendered = json.dumps(details, sort_keys=True)

    assert receipt["status"] == "invalid"
    assert receipt["source"] == "file"
    assert receipt["error_code"] == "PRODUCTION_READINESS_PROOF_FILE_INVALID"
    assert receipt["path"] == "[redacted-path]"
    assert receipt["reason"] == "Target file must be a regular file: [redacted-path]"
    assert details["status"] == "invalid"
    assert details["source"] == "file"
    assert details["error_code"] == "PRODUCTION_READINESS_PROOF_FILE_INVALID"
    assert str(proof_file) not in rendered
    assert str(proof_file.parent) not in rendered
    assert "private" not in rendered
    assert "[redacted-path]" in rendered


@pytest.mark.parametrize("source", ["inline", "file"])
def test_live_proof_loader_oversized_payload_has_bounded_redacted_preview(
    tmp_path: Path,
    source: str,
) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    oversized = (
        '{"signed_uri":"https://user:pass@alerts.example.invalid/hook?token=secret",'
        '"local_path":"/tmp/private-live-proof/receipt.json",'
        f'"blob":"{"x" * readiness_shared_artifacts.MAX_RECEIPT_BYTES}"}}'
    )
    proof_file = None
    proof_json = oversized
    if source == "file":
        proof_file = tmp_path / "oversized-alert-proof.json"
        proof_file.write_bytes(oversized.encode("utf-8"))
        proof_json = None

    receipt = readiness_shared_artifacts._load_proof("alert", proof_json, proof_file, config=config)
    preview = receipt["raw_preview"]

    assert receipt["status"] == "too_large"
    assert receipt["error_code"] == "PRODUCTION_READINESS_PROOF_TOO_LARGE"
    assert "token=secret" not in preview
    assert "user:pass@" not in preview
    assert "/tmp/private-live-proof/receipt.json" not in preview
    assert "[truncated]" in preview
    assert len(preview.encode("utf-8")) <= readiness_shared_artifacts.MAX_RECEIPT_PREVIEW_BYTES + 512


@pytest.mark.parametrize(
    ("proof_json", "proof_bytes"),
    [
        ("{not-json-token=secret", None),
        ("[1, 2, 3]", None),
        (None, b'\xff{"schema": "nhms.production_readiness.live_proof.v1"}'),
    ],
)
def test_live_proof_loader_json_invalid_cases_are_bounded(
    tmp_path: Path,
    proof_json: str | None,
    proof_bytes: bytes | None,
) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    proof_file = None
    if proof_bytes is not None:
        proof_file = tmp_path / "invalid-utf8-proof.json"
        proof_file.write_bytes(proof_bytes)

    receipt = readiness_shared_artifacts._load_proof("alert", proof_json, proof_file, config=config)
    rendered = json.dumps(readiness_shared_artifacts._receipt_details(receipt, config=config), sort_keys=True)

    assert receipt["status"] == "invalid"
    assert receipt["error_code"] == "PRODUCTION_READINESS_PROOF_JSON_INVALID"
    assert "raw_preview" in receipt
    assert "not-json-token=secret" not in rendered
    assert "Traceback" not in rendered


@pytest.mark.parametrize("overflow", ["node", "depth"])
def test_live_proof_loader_json_limit_output_is_bounded(tmp_path: Path, overflow: str) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    payload = json.loads(_bound_proof("alert"))
    payload["local_path"] = "/tmp/private-live-proof/receipt.json"
    if overflow == "node":
        payload["node_overflow"] = [{"i": index} for index in range(1300)]
        expected_error = "json_node_limit_exceeded"
    else:
        nested: dict[str, object] = {"value": "too deep"}
        for index in range(20):
            nested = {f"level_{index}": nested}
        payload["depth_overflow"] = nested
        expected_error = "json_depth_limit_exceeded"

    receipt = readiness_shared_artifacts._load_proof("alert", json.dumps(payload), None, config=config)
    rendered = json.dumps(readiness_shared_artifacts._receipt_details(receipt, config=config), sort_keys=True)

    assert receipt["status"] == "invalid"
    assert receipt["error_code"] == "PRODUCTION_READINESS_PROOF_JSON_LIMIT_EXCEEDED"
    assert expected_error in receipt["json_limit_errors"]
    assert "raw_payload" not in receipt
    assert "[truncated:max-" in rendered
    assert "/tmp/private-live-proof/receipt.json" not in rendered
    assert len(rendered.encode("utf-8")) < 64 * 1024


def test_shared_artifact_preflight_payload_pins_schema_paths_and_side_effect_policy(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = root / "scheduler"
    config = ProductionReadinessConfig.from_env(
        evidence_root=root,
        run_id="m19",
        slurm_evidence_root=root / "slurm",
        scheduler_evidence_root=scheduler_root,
        scheduler_evidence_file=scheduler_root / "pass.json",
    )
    receipts = {
        "auth": {"status": "missing"},
        "alert": {"status": "parse_failed"},
        "slurm": {"status": "parsed"},
    }

    payload = readiness_shared_artifacts._preflight_payload(config, receipts)

    assert payload == readiness_validation._preflight_payload(config, receipts)
    assert payload["schema"] == "nhms.production_readiness.preflight.v1"
    assert payload["issue"] == 181
    assert payload["run_id"] == "m19"
    assert payload["evidence_root"] == "evidence-root"
    assert payload["evidence_dir"] == "readiness"
    assert payload["dependency_roots"]["slurm"] == "evidence-root/slurm"
    assert payload["dependency_roots"]["object_store"] is None
    assert payload["scheduler_evidence_root"] == "evidence-root/scheduler"
    assert payload["scheduler_evidence_file"] == "evidence-root/scheduler/pass.json"
    assert payload["live_proof_configured"] == {
        "auth": False,
        "alert": True,
        "slurm": True,
    }
    assert payload["fast_ci_live_side_effect_policy"] == {
        "executes_live_idp": False,
        "executes_live_alert_sink": False,
        "executes_backend_mutation": False,
        "executes_live_rollback": False,
        "executes_live_slurm": False,
        "executes_live_object_store": False,
        "executes_live_weather_source": False,
        "executes_real_national_data": False,
    }


def test_shared_artifact_path_helper_prefixes_and_redacted_fallback(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")

    assert readiness_shared_artifacts._path_for_evidence(config.lane_dir, config=config) == "readiness"
    assert (
        readiness_shared_artifacts._path_for_evidence(config.lane_dir / "preflight.json", config=config)
        == "readiness/preflight.json"
    )
    assert readiness_shared_artifacts._path_for_evidence(config.evidence_root, config=config) == "evidence-root"
    assert (
        readiness_shared_artifacts._path_for_evidence(config.evidence_root / "m19", config=config)
        == "evidence-root/m19"
    )
    assert (
        readiness_shared_artifacts._path_for_evidence(Path.cwd() / "docs" / "governance", config=config)
        == "workspace/docs/governance"
    )
    assert (
        readiness_shared_artifacts._path_for_evidence(tmp_path / "private" / "secret.json", config=config)
        == "[redacted-path]"
    )
    assert (
        readiness_validation._path_for_evidence(config.lane_dir / "preflight.json", config=config)
        == "readiness/preflight.json"
    )


def test_dependency_summary_owner_facade_aliases_are_stable(tmp_path: Path) -> None:
    summary_root = tmp_path / "slurm"
    _write_dependency_summary(summary_root, "slurm")
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        slurm_evidence_root=summary_root,
    )

    assert readiness_validation.DEPENDENCY_SUMMARY_CONTRACTS is (
        readiness_dependency_summaries.DEPENDENCY_SUMMARY_CONTRACTS
    )
    assert readiness_validation._dependency_summary_blocked is (
        readiness_dependency_summaries._dependency_summary_blocked
    )
    assert readiness_validation._dependency_summary_artifact_ref is (
        readiness_dependency_summaries._dependency_summary_artifact_ref
    )
    assert readiness_validation._dependency_bindings is readiness_dependency_summaries._dependency_bindings
    assert readiness_validation._find_summary_path is readiness_dependency_summaries._find_summary_path
    assert readiness_validation._read_dependency_summary_item("slurm", summary_root, config=config) == (
        readiness_dependency_summaries._read_dependency_summary_item("slurm", summary_root, config=config)
    )
    assert readiness_validation._dependency_summary_items(config) == (
        readiness_dependency_summaries._dependency_summary_items(config)
    )


def test_scheduler_evidence_owner_facade_aliases_and_outputs_match(tmp_path: Path) -> None:
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(scheduler_path)
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        scheduler_evidence_file=scheduler_path,
    )

    for name in (
        "SCHEDULER_EVIDENCE_SCHEMA",
        "MAX_SCHEDULER_EVIDENCE_BYTES",
        "MAX_SCHEDULER_EVIDENCE_FILES",
        "SCHEDULER_REVIEW_EXECUTION_MODES",
        "SCHEDULER_REVIEW_PASSED_STATUSES",
        "SCHEDULER_REVIEW_BLOCKED_STATUSES",
        "SCHEDULER_REQUIRED_COUNT_FIELDS",
        "SCHEDULER_DRY_RUN_NO_MUTATION_FALSE_FIELDS",
        "SCHEDULER_LIVE_PRODUCER_EXECUTION_MODES",
        "SCHEDULER_LIVE_WORK_STATUSES",
    ):
        assert getattr(readiness_validation, name) is getattr(readiness_scheduler_evidence, name)
    for name in (
        "_scheduler_evidence_blocked",
        "_scheduler_bindings",
        "_safe_scheduler_evidence_file",
        "_scheduler_evidence_errors",
        "_scheduler_readiness_status",
        "_scheduler_evidence_mode",
        "_scheduler_evidence_artifact_ref",
        "_scheduler_item_suffix",
    ):
        assert getattr(readiness_validation, name) is getattr(readiness_scheduler_evidence, name)

    assert readiness_validation._read_scheduler_evidence_item(scheduler_path, config=config) == (
        readiness_scheduler_evidence._read_scheduler_evidence_item(scheduler_path, config=config)
    )
    assert readiness_validation._scheduler_evidence_items(config) == (
        readiness_scheduler_evidence._scheduler_evidence_items(config)
    )


def test_scheduler_evidence_facade_read_item_honors_facade_safe_file_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_path = tmp_path / "configured" / "scheduler.json"
    alternate_path = tmp_path / "alternate" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(alternate_path)
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        scheduler_evidence_file=configured_path,
    )

    def fake_safe_scheduler_evidence_file(path: Path) -> Path:
        assert path == configured_path
        return alternate_path

    monkeypatch.setattr(readiness_validation, "_safe_scheduler_evidence_file", fake_safe_scheduler_evidence_file)

    item = readiness_validation._read_scheduler_evidence_item(configured_path, config=config)

    assert item["status"] == "passed"
    assert item["artifact_refs"] == [readiness_shared_artifacts._path_for_evidence(alternate_path, config=config)]


def test_scheduler_evidence_facade_read_item_honors_facade_mode_in_acceptance_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler_path = tmp_path / "scheduler" / "scheduler_20260521120000_fixed.json"
    payload = _scheduler_evidence_payload(
        execution_mode="deterministic",
        no_mutation_proof={"adapter_download_called": False},
    )
    _write_scheduler_payload(scheduler_path, payload)
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        scheduler_evidence_file=scheduler_path,
    )

    monkeypatch.setattr(readiness_validation, "_scheduler_evidence_mode", lambda _payload: "dry_run")

    item = readiness_validation._read_scheduler_evidence_item(scheduler_path, config=config)

    assert item["status"] == "blocked"
    assert item["details"]["scheduler_execution_mode"] == "dry_run"
    assert "dry_run_no_mutation_proof_missing" in item["details"]["acceptance_errors"]


def test_scheduler_evidence_facade_items_honors_facade_reader_and_finder_monkeypatches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "configured-scheduler"
    alternate_path = tmp_path / "alternate-scheduler" / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(alternate_path)
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        scheduler_evidence_root=configured_root,
    )

    def fake_find_scheduler_evidence_files(root: Path) -> list[Path]:
        assert root == configured_root
        return [alternate_path]

    def fake_read_scheduler_evidence_item(path: Path, *, config: ProductionReadinessConfig) -> dict[str, object]:
        assert path == alternate_path
        return readiness_validation._scheduler_evidence_blocked(
            path,
            config=config,
            reason="patched facade scheduler reader",
        )

    monkeypatch.setattr(readiness_validation, "_find_scheduler_evidence_files", fake_find_scheduler_evidence_files)
    monkeypatch.setattr(readiness_validation, "_read_scheduler_evidence_item", fake_read_scheduler_evidence_item)

    item = next(
        item
        for item in readiness_validation._scheduler_evidence_items(config)
        if item["surface"] == "scheduler_production_like_evidence"
    )

    assert item["status"] == "blocked"
    assert item["residual_risk"] == "patched facade scheduler reader"


def test_dependency_summary_facade_read_item_honors_facade_find_path_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "configured-slurm"
    alternate_root = tmp_path / "alternate-slurm"
    _write_dependency_summary(alternate_root, "slurm")
    alternate_path = alternate_root / "summary.json"
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        slurm_evidence_root=configured_root,
    )

    def fake_find_summary_path(name: str, root: Path) -> Path:
        assert name == "slurm"
        assert root == configured_root
        return alternate_path

    monkeypatch.setattr(readiness_validation, "_find_summary_path", fake_find_summary_path)

    item = readiness_validation._read_dependency_summary_item("slurm", configured_root, config=config)

    assert item["status"] == "passed"
    assert item["details"]["summary_run_id"] == "slurm-live-run"


def test_dependency_summary_facade_items_honors_facade_read_item_monkeypatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    summary_root = tmp_path / "slurm"
    _write_dependency_summary(summary_root, "slurm")
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        slurm_evidence_root=summary_root,
    )

    def fake_read_item(name: str, root: Path, *, config: ProductionReadinessConfig) -> dict[str, object]:
        return readiness_validation._dependency_summary_blocked(
            name,
            root,
            config=config,
            reason="patched facade summary reader",
        )

    monkeypatch.setattr(readiness_validation, "_read_dependency_summary_item", fake_read_item)

    item = next(
        item
        for item in readiness_validation._dependency_summary_items(config)
        if item["surface"] == "slurm_production_like_evidence"
    )

    assert item["status"] == "blocked"
    assert item["residual_risk"] == "patched facade summary reader"


def test_dependency_summary_missing_unconfigured_roots_are_not_executed(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")

    items = readiness_dependency_summaries._dependency_summary_items(config)

    assert len(items) == len(readiness_dependency_summaries.DEPENDENCY_SUMMARY_CONTRACTS)
    assert {item["status"] for item in items} == {"not_executed"}
    assert {item["execution_mode"] for item in items} == {"not_executed"}
    assert all(item["required_for_final"] is False for item in items)
    assert all(item["live_proof_accepted"] is False for item in items)
    assert all(item["artifact_refs"] == [] for item in items)
    assert readiness_validation._dependency_bindings(items) == {}


@pytest.mark.parametrize(
    ("proof_key", "alias_dir", "expected_ref"),
    [
        ("slurm", "slurm", "slurm:slurm/summary.json"),
        ("object_store", "object_store", "object_store:object_store/summary.json"),
        ("object_store", "object-store", "object_store:object-store/summary.json"),
        ("source", "source", "source:source/summary.json"),
        ("e2e", "e2e", "e2e:e2e/summary.json"),
        ("mvt", "mvt", "mvt:mvt/summary.json"),
    ],
)
def test_dependency_summary_nested_aliases_are_preserved(
    tmp_path: Path,
    proof_key: str,
    alias_dir: str,
    expected_ref: str,
) -> None:
    summary_root = tmp_path / f"{proof_key}-root"
    summary_path = summary_root / alias_dir / "summary.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(json.dumps(_dependency_summary_payload(proof_key)), encoding="utf-8")
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        **{f"{proof_key}_evidence_root": summary_root},
    )

    item = readiness_dependency_summaries._read_dependency_summary_item(proof_key, summary_root, config=config)

    assert readiness_validation._find_summary_path(proof_key, summary_root) == summary_path
    assert item["status"] == "passed"
    assert item["execution_mode"] == "deterministic"
    assert item["details"]["producer_artifact_ref"] == expected_ref
    assert item["details"]["summary_checksum"].startswith("sha256:")
    assert readiness_validation._dependency_bindings([item])[proof_key]["summary_run_id"] == (
        f"{proof_key}-live-run"
    )


def test_dependency_summary_slurm_submitted_status_is_accepted_without_final_readiness(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "slurm"
    summary_root.mkdir()
    payload = _dependency_summary_payload("slurm") | {"status": "submitted"}
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    assert item["status"] == "passed"
    assert item["execution_mode"] == "deterministic"
    assert item["live_proof_accepted"] is False
    assert item["required_for_final"] is False
    assert item["details"]["summary_status"] == "submitted"
    assert item["details"]["producer_artifact_ref"] == "slurm:summary.json"
    assert item["details"]["summary_checksum"].startswith("sha256:")
    assert "summary_status=submitted" in item["dependencies"]
    assert "producer_artifact_ref=slurm:summary.json" in item["dependencies"]
    assert any(dependency.startswith("summary_checksum=sha256:") for dependency in item["dependencies"])
    assert readiness_validation._dependency_bindings([item])["slurm"]["summary_run_id"] == "slurm-live-run"
    assert _summary(root)["status"] == "release_blocked"
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    "proof_key",
    [
        pytest.param("object_store", id="object-store-summary"),
        pytest.param("source", id="source-summary"),
        pytest.param("e2e", id="end-to-end-summary"),
        pytest.param("mvt", id="mvt-summary"),
    ],
)
def test_dependency_summary_non_slurm_submitted_status_is_blocked_unbound_without_final_readiness(
    tmp_path: Path,
    proof_key: str,
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / f"{proof_key}-summary"
    summary_root.mkdir()
    payload = _dependency_summary_payload(proof_key) | {"status": "submitted"}
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            **{f"{proof_key}_evidence_root": summary_root},
        )
    )

    item = next(item for item in _items(root) if item["surface"] == f"{proof_key}_production_like_evidence")
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["live_proof_accepted"] is False
    assert item["required_for_final"] is False
    assert item["details"]["summary_status"] == "submitted"
    assert "summary_status=submitted" in item["dependencies"]
    assert readiness_validation._dependency_bindings([item]) == {}
    assert _summary(root)["status"] == "release_blocked"
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_dependency_bindings_include_only_passed_dependency_summaries(tmp_path: Path) -> None:
    passed_root = tmp_path / "slurm"
    blocked_root = tmp_path / "source"
    _write_dependency_summary(passed_root, "slurm")
    blocked_root.mkdir()
    blocked_payload = _dependency_summary_payload("source") | {"status": "blocked"}
    (blocked_root / "summary.json").write_text(json.dumps(blocked_payload), encoding="utf-8")
    config = ProductionReadinessConfig.from_env(
        evidence_root=tmp_path / "artifacts",
        run_id="m19",
        slurm_evidence_root=passed_root,
        source_evidence_root=blocked_root,
    )
    items = readiness_dependency_summaries._dependency_summary_items(config)

    bindings = readiness_validation._dependency_bindings(items)

    assert set(bindings) == {"slurm"}
    slurm_binding = bindings["slurm"]
    assert slurm_binding["dependency"] == "slurm"
    assert slurm_binding["summary_run_id"] == "slurm-live-run"
    assert slurm_binding["producer_artifact_ref"] == "slurm:summary.json"
    assert slurm_binding["summary_checksum"].startswith("sha256:")


@pytest.mark.parametrize(
    ("case_name", "expected_public_run_id", "forbidden_fragment"),
    [
        ("missing", None, None),
        ("blank", "[invalid-run-id]", None),
        ("path_like_private_path", "[redacted-path]", "private-run-ids"),
        ("oversized_truncated", "[invalid-run-id][truncated]", "raw-oversized-run-id-tail"),
    ],
)
def test_dependency_summary_run_id_contract_failures_are_blocked_unbound_and_redacted(
    tmp_path: Path,
    case_name: str,
    expected_public_run_id: object,
    forbidden_fragment: str | None,
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "slurm"
    summary_root.mkdir()
    payload = _dependency_summary_payload("slurm")
    raw_run_id = None
    if case_name == "missing":
        payload.pop("run_id")
    elif case_name == "blank":
        raw_run_id = "   "
        payload["run_id"] = raw_run_id
    elif case_name == "path_like_private_path":
        raw_run_id = str(tmp_path / "private-run-ids" / "slurm-live-run")
        payload["run_id"] = raw_run_id
    elif case_name == "oversized_truncated":
        raw_run_id = (
            "slurm-live-run-"
            + ("x" * (readiness_shared_artifacts.MAX_STRING_LENGTH + 512))
            + "raw-oversized-run-id-tail"
        )
        payload["run_id"] = raw_run_id
    else:
        raise AssertionError(f"unhandled case: {case_name}")
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").glob("*.json")
    )
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["required_for_final"] is False
    assert item["live_proof_accepted"] is False
    assert item["details"]["summary_run_id"] == expected_public_run_id
    assert readiness_validation._dependency_bindings([item]) == {}
    assert readiness_validation._dependency_bindings([item | {"status": "passed"}]) == {}
    if raw_run_id and raw_run_id.strip():
        assert raw_run_id not in artifact_text
    if forbidden_fragment is not None:
        assert forbidden_fragment not in artifact_text
    assert _summary(root)["status"] == "release_blocked"
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("case_name", "writer", "expected_fragment"),
    [
        (
            "missing_summary",
            lambda summary_root: summary_root.mkdir(parents=True),
            "Dependency summary could not be read",
        ),
        (
            "malformed_json",
            lambda summary_root: _write_text(summary_root / "summary.json", "{not-json-token=secret"),
            "Dependency summary could not be read",
        ),
        (
            "invalid_utf8",
            lambda summary_root: _write_bytes(summary_root / "summary.json", b"\xff\xfe"),
            "Dependency summary could not be read",
        ),
        (
            "non_object_json",
            lambda summary_root: _write_text(summary_root / "summary.json", "[]"),
            "Dependency summary JSON must be an object",
        ),
        (
            "oversized_payload",
            lambda summary_root: _write_text(
                summary_root / "summary.json",
                json.dumps(_dependency_summary_payload("slurm") | {"blob": "x" * (70 * 1024)}),
            ),
            "Dependency summary exceeds bounded readiness ingestion limit",
        ),
        (
            "directory_leaf",
            lambda summary_root: (summary_root / "summary.json").mkdir(parents=True),
            "regular file",
        ),
        (
            "symlink_leaf",
            lambda summary_root: _write_symlink_summary(summary_root, leaf=True),
            "symlink",
        ),
        (
            "symlink_ancestor",
            lambda summary_root: _write_symlink_summary(summary_root, leaf=False),
            "symlink",
        ),
    ],
)
def test_dependency_summary_boundary_failures_are_blocked_redacted_and_bounded(
    tmp_path: Path,
    case_name: str,
    writer: Callable[[Path], None],
    expected_fragment: str,
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "private" / case_name / "slurm"
    writer(summary_root)

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    artifact_text = json.dumps(item, sort_keys=True) + "\n" + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").iterdir()
    )
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["live_proof_accepted"] is False
    assert expected_fragment in artifact_text
    assert str(summary_root) not in artifact_text
    assert "not-json-token=secret" not in artifact_text
    assert "Traceback" not in artifact_text
    assert len(artifact_text) < 80_000
    assert _summary(root)["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("override", "expected_detail"),
    [
        ({"issue": 999}, "summary_issue"),
        ({"schema": "nhms.production_closure.slurm.v0"}, "summary_schema"),
        ({"status": "failed"}, "summary_status"),
    ],
)
def test_dependency_summary_wrong_contract_values_are_blocked_with_public_details(
    tmp_path: Path,
    override: dict[str, object],
    expected_detail: str,
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "private" / "slurm"
    summary_root.mkdir(parents=True)
    payload = _dependency_summary_payload("slurm") | override
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    rendered = json.dumps(item, sort_keys=True)
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert expected_detail in rendered
    assert "Existing production-closure summary is missing, malformed, or outside the expected contract." in rendered
    assert str(summary_root) not in rendered
    assert item["details"]["summary_checksum"].startswith("sha256:")
    assert _summary(root)["status"] == "release_blocked"


@pytest.mark.parametrize("case_name", ["object", "list"])
def test_dependency_summary_non_string_status_is_blocked_unbound_and_redacted(
    tmp_path: Path,
    case_name: str,
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "slurm-summary"
    summary_root.mkdir()
    raw_status_path = tmp_path / "private-status-marker" / case_name / "summary.json"
    raw_status_secret = f"raw-status-secret-{case_name}"
    if case_name == "object":
        raw_status: object = {
            "state": "ready",
            "path": str(raw_status_path),
            "token": raw_status_secret,
        }
    elif case_name == "list":
        raw_status = ["ready", str(raw_status_path), {"token": raw_status_secret}]
    else:
        raise AssertionError(f"unhandled case: {case_name}")
    payload = _dependency_summary_payload("slurm") | {"status": raw_status}
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", slurm_evidence_root=summary_root)
    )

    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    artifact_text = json.dumps(item, sort_keys=True) + "\n" + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").glob("*.json")
    )
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert item["live_proof_accepted"] is False
    assert item["required_for_final"] is False
    assert item["details"]["summary_status"] == "[invalid-status-type]"
    assert "summary_status=[invalid-status-type]" in item["dependencies"]
    assert readiness_validation._dependency_bindings([item]) == {}
    assert _summary(root)["status"] == "release_blocked"
    assert _summary(root)["final_production_readiness_claimed"] is False
    assert "[invalid-status-type]" in artifact_text
    assert str(raw_status_path) not in artifact_text
    assert "private-status-marker" not in artifact_text
    assert raw_status_secret not in artifact_text


@pytest.mark.parametrize("proof_key", DEPENDENCY_PROOF_PARAMS)
def test_dependency_summary_rejected_status_redacts_paths_and_secrets_from_public_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    proof_key: str,
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / f"{proof_key}-summary"
    summary_root.mkdir()
    raw_status_path = tmp_path / "private-status-marker" / proof_key / "summary.json"
    raw_status = f"{raw_status_path}?api_token=raw-status-secret"
    payload = _dependency_summary_payload(proof_key) | {"status": raw_status}
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            DEPENDENCY_EVIDENCE_ROOT_OPTIONS[proof_key],
            str(summary_root),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    item = next(item for item in _items(root) if item["surface"] == f"{proof_key}_production_like_evidence")
    artifact_text = captured.out + captured.err + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").glob("*.json")
    )
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert "summary_status=[redacted-path]" in item["dependencies"]
    assert raw_status not in artifact_text
    assert str(raw_status_path) not in artifact_text
    assert "private-status-marker" not in artifact_text
    assert "raw-status-secret" not in artifact_text
    assert "[redacted-path]" in artifact_text
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_dependency_summary_rejected_oversized_status_is_bounded_in_public_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    summary_root = tmp_path / "slurm"
    summary_root.mkdir()
    raw_tail = "raw-oversized-status-tail"
    oversized_status = "oversized-status-" + ("x" * (readiness_shared_artifacts.MAX_STRING_LENGTH + 512)) + raw_tail
    payload = _dependency_summary_payload("slurm") | {"status": oversized_status}
    (summary_root / "summary.json").write_text(json.dumps(payload), encoding="utf-8")

    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            "--slurm-evidence-root",
            str(summary_root),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    item = next(item for item in _items(root) if item["surface"] == "slurm_production_like_evidence")
    status_dependency = next(
        dependency for dependency in item["dependencies"] if dependency.startswith("summary_status=")
    )
    artifact_text = captured.out + captured.err + "\n".join(
        path.read_text(encoding="utf-8") for path in (root / "m19" / "readiness").glob("*.json")
    )
    assert item["status"] == "blocked"
    assert item["execution_mode"] == "not_executed"
    assert status_dependency.endswith("[truncated]")
    assert item["details"]["summary_status"].endswith("[truncated]")
    assert len(status_dependency) <= (
        len("summary_status=") + readiness_shared_artifacts.MAX_STRING_LENGTH + len("[truncated]")
    )
    assert oversized_status not in artifact_text
    assert raw_tail not in artifact_text
    assert "[truncated]" in artifact_text
    assert len(artifact_text) < 80_000
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_shared_artifact_safe_writer_guards_no_clobber_force_symlink_containment_payload_and_redaction(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    lane = root / "m19" / "readiness"
    writer = readiness_shared_artifacts.EvidenceWriter(root, lane)
    writer.prepare()
    output = lane / "payload.json"
    writer.write_json(
        output,
        {
            "AUTH_TOKEN": "token=secret",
            "AWS_SECRET_ACCESS_KEY": "super-secret",
            "DATABASE_URL": "postgresql://user:password@db.internal:55432/nhms",
        },
    )
    rendered = output.read_text(encoding="utf-8")
    assert "token=secret" not in rendered
    assert "super-secret" not in rendered
    assert "password" not in rendered

    with pytest.raises(ProductionReadinessValidationError) as existing:
        readiness_shared_artifacts.EvidenceWriter(root, lane).write_json(output, {"status": "blocked"})
    assert existing.value.error_code == "PRODUCTION_READINESS_EVIDENCE_EXISTS"

    readiness_shared_artifacts.EvidenceWriter(root, lane, force=True).write_json(output, {"status": "overwritten"})
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "overwritten"}

    symlink_path = lane / "symlink.json"
    symlink_target = tmp_path / "target.json"
    symlink_target.write_text("{}", encoding="utf-8")
    symlink_path.symlink_to(symlink_target)
    with pytest.raises(ProductionReadinessValidationError) as symlink_error:
        readiness_shared_artifacts.EvidenceWriter(root, lane, force=True).write_json(symlink_path, {"status": "bad"})
    assert symlink_error.value.error_code == "PRODUCTION_READINESS_EVIDENCE_SYMLINK"

    with pytest.raises(ProductionReadinessValidationError) as containment_error:
        readiness_shared_artifacts.EvidenceWriter(root, lane, force=True).write_json(
            lane.parent / "outside.json", {"status": "bad"}
        )
    assert containment_error.value.error_code == "PRODUCTION_READINESS_EVIDENCE_PATH_UNSAFE"

    with pytest.raises(ProductionReadinessValidationError) as payload_error:
        readiness_shared_artifacts.EvidenceWriter(root, lane, force=True, max_payload_bytes=32).write_json(
            lane / "too-large.json", {"blob": "x" * 80}
        )
    assert payload_error.value.error_code == "PRODUCTION_READINESS_EVIDENCE_PAYLOAD_TOO_LARGE"


def test_shared_artifact_safe_writer_rejects_non_force_create_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = (tmp_path / "artifacts").resolve()
    lane = root / "m19" / "readiness"
    writer = readiness_shared_artifacts.EvidenceWriter(root, lane)
    writer.prepare()
    output = lane / "race.json"
    original_create = readiness_shared_artifacts.write_bytes_no_follow_exclusive

    def create_target_before_exclusive_write(path: Path, content: bytes, **kwargs: object) -> Path:
        path.write_text('{"status": "competing-writer"}\n', encoding="utf-8")
        return original_create(path, content, **kwargs)

    monkeypatch.setattr(
        readiness_shared_artifacts,
        "write_bytes_no_follow_exclusive",
        create_target_before_exclusive_write,
    )

    with pytest.raises(ProductionReadinessValidationError) as error:
        writer.write_json(output, {"status": "current-writer"})

    assert error.value.error_code == "PRODUCTION_READINESS_EVIDENCE_EXISTS"
    assert json.loads(output.read_text(encoding="utf-8")) == {"status": "competing-writer"}


def test_shared_artifact_bounded_payload_caps_wide_mapping_output(tmp_path: Path) -> None:
    config = ProductionReadinessConfig.from_env(evidence_root=tmp_path / "artifacts", run_id="m19")
    wide_payload = {f"key-{index}": index for index in range(readiness_shared_artifacts.MAX_JSON_NODES + 25)}

    bounded = readiness_shared_artifacts._bounded_payload(wide_payload)
    redacted = readiness_shared_artifacts._bounded_redacted_payload(wide_payload, config=config)

    assert isinstance(bounded.payload, dict)
    assert len(bounded.payload) <= readiness_shared_artifacts.MAX_JSON_NODES
    assert bounded.node_truncated is True
    assert list(bounded.payload.values()).count("[truncated:max-nodes]") == 1
    assert isinstance(redacted, dict)
    assert len(redacted) <= readiness_shared_artifacts.MAX_JSON_NODES
    assert list(redacted.values()).count("[truncated:max-nodes]") == 1


def test_status_execution_mode_truth_table_accepts_allowed_and_rejects_forbidden() -> None:
    assert STATUS_VALUES == frozenset(EXPECTED_ALLOWED_STATUS_EXECUTION_MODES)
    assert EXECUTION_MODE_VALUES == EXPECTED_EXECUTION_MODE_VALUES
    assert EXECUTED_MODES == EXPECTED_EXECUTION_MODE_VALUES - {"not_executed"}
    assert ALLOWED_STATUS_EXECUTION_MODES == EXPECTED_ALLOWED_STATUS_EXECUTION_MODES

    validators = (
        readiness_item_contracts.validate_readiness_item,
        readiness_validation.validate_readiness_item,
    )
    for validator in validators:
        for status, modes in EXPECTED_ALLOWED_STATUS_EXECUTION_MODES.items():
            for mode in modes:
                validator(_base_item(status, mode))

        for status, modes in EXPECTED_ALLOWED_STATUS_EXECUTION_MODES.items():
            forbidden = EXPECTED_EXECUTION_MODE_VALUES - modes
            for mode in forbidden:
                _assert_contract_error(
                    validator,
                    _base_item(status, mode),
                    "PRODUCTION_READINESS_STATUS_MODE_INVALID",
                )

        _assert_contract_error(
            validator,
            _base_item("not-a-status", "deterministic"),
            "PRODUCTION_READINESS_STATUS_INVALID",
        )
        _assert_contract_error(
            validator,
            _base_item("passed", "not-a-mode"),
            "PRODUCTION_READINESS_EXECUTION_MODE_INVALID",
        )
        for field in EXPECTED_REQUIRED_READINESS_ITEM_FIELDS:
            missing = _base_item("passed", "deterministic")
            missing.pop(field)
            _assert_contract_error(
                validator,
                missing,
                "PRODUCTION_READINESS_ITEM_FIELD_MISSING",
            )
        missing_context = _base_item("release_blocked", "not_executed")
        missing_context["required_for_final"] = True
        missing_context["residual_risk"] = ""
        _assert_contract_error(
            validator,
            missing_context,
            "PRODUCTION_READINESS_BLOCKER_CONTEXT_MISSING",
        )


def test_readiness_schema_validation_item_is_emitted_for_invalid_item_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    invalid_item = _base_item("release_blocked", "not_executed")
    invalid_item["required_for_final"] = True
    invalid_item.pop("item_id")
    monkeypatch.setattr(readiness_validation, "_deterministic_items", lambda config: [invalid_item])

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19"))

    validation_item = next(item for item in _items(root) if item["surface"] == "readiness_schema_validation")
    assert validation_item["item_id"] == "schema-validation-0"
    assert validation_item["status"] == "failed"
    assert validation_item["execution_mode"] == "deterministic"
    assert validation_item["required_for_final"] is False
    assert validation_item["artifact_refs"] == ["readiness_items.json"]
    assert "item_id" in validation_item["residual_risk"]
    blockers = _blockers(root)
    assert any(blocker["surface"] == "readiness_schema_validation" for blocker in blockers)
    assert all(blocker["surface"] != "unit" for blocker in blockers)
    assert _summary(root)["final_production_readiness_claimed"] is False


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


def test_incomplete_live_auth_receipt_is_redacted_and_remains_release_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    auth_receipt = _auth_proof(
        allowed=["models.activate", "jobs.cancel", {"provider": "opaque-permission-token"}],
        denied=["users.manage"],
        value="opaque-live-token",
        errors=[{"status": "opaque-error-token"}],
        scope={
            "provider": "opaque-provider-token",
            "status": "opaque-status-token",
            "message": "opaque-scope-token",
        },
        roles=["operator", {"provider": "opaque-role-token"}],
    )

    exit_code = slurm_validation.main(
        ["validate-readiness", "--evidence-root", str(root), "--run-id", "m19", "--auth-proof", auth_receipt]
    )

    assert exit_code == 0
    stdout = capsys.readouterr().out
    auth_item = next(item for item in _items(root) if item["surface"] == "live_backend_auth")
    assert auth_item["status"] == "release_blocked"
    assert auth_item["execution_mode"] == "live_proof"
    assert auth_item["live_proof_accepted"] is False
    assert "missing_allowed_actions" in auth_item["details"]["acceptance_errors"]
    assert _summary(root)["final_production_readiness_claimed"] is False

    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    items = (root / "m19" / "readiness" / "readiness_items.json").read_text(encoding="utf-8")
    summary = (root / "m19" / "readiness" / "summary.json").read_text(encoding="utf-8")
    for artifact in (receipts, items, summary, stdout):
        for raw_secret in (
            "super-secret",
            "token=secret",
            "user:pass@",
            "opaque-live-token",
            "opaque-error-token",
            "opaque-provider-token",
            "opaque-status-token",
            "opaque-scope-token",
            "opaque-permission-token",
            "opaque-role-token",
        ):
            assert raw_secret not in artifact

    for detailed_artifact in (receipts, items):
        assert "https://idp.example.invalid/auth" in detailed_artifact
        assert "models.activate" in detailed_artifact
        assert "jobs.cancel" in detailed_artifact
        assert "users.manage" in detailed_artifact
        assert "pipeline.retry_run" in detailed_artifact
        assert "model_admin" in detailed_artifact


def test_validate_readiness_write_order_preserves_preflight_receipts_then_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    write_names: list[str] = []
    original_write_json = readiness_shared_artifacts.EvidenceWriter.write_json

    def recording_write_json(
        self: readiness_shared_artifacts.EvidenceWriter,
        path: Path,
        payload: object,
    ) -> None:
        write_names.append(path.name)
        original_write_json(self, path, payload)

    monkeypatch.setattr(readiness_shared_artifacts.EvidenceWriter, "write_json", recording_write_json)

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19"))

    assert write_names[:3] == [
        "preflight.json",
        "live_proof_receipts.json",
        "readiness_items.json",
    ]
    readiness_dir = root / "m19" / "readiness"
    assert (readiness_dir / "preflight.json").is_file()
    assert (readiness_dir / "live_proof_receipts.json").is_file()
    assert (readiness_dir / "readiness_items.json").is_file()


def test_contract_mismatched_live_auth_receipt_is_redacted_and_remains_release_blocked(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "artifacts"
    malformed_auth_receipt = json.dumps(
        {
            "schema": LIVE_SCHEMA,
            "status": "passed",
            "accepted": True,
            "run_id": "m19",
            "target_environment": "production",
            "execution_mode": "live_proof",
            "artifact_refs": ["evidence/m19/auth/receipt.json"],
            "provider": {
                "issuer_url": "https://user:pass@idp.example.invalid/auth?token=secret",
                "client_secret": "super-secret",
            },
            "role_mapping": {"operator": ["pipeline.retry_run"]},
            "allowed_actions": _all_auth_actions(),
            "denied_actions": _all_auth_actions(),
            "value": "opaque-live-token",
            "errors": [{"status": "opaque-error-token"}],
            "scope": {
                "provider": "opaque-provider-token",
                "status": "opaque-status-token",
                "message": "opaque-scope-token",
            },
        }
    )

    exit_code = slurm_validation.main(
        [
            "validate-readiness",
            "--evidence-root",
            str(root),
            "--run-id",
            "m19",
            "--auth-proof",
            malformed_auth_receipt,
        ]
    )

    assert exit_code == 0
    stdout = capsys.readouterr().out
    auth_item = next(item for item in _items(root) if item["surface"] == "live_backend_auth")
    assert auth_item["status"] == "release_blocked"
    assert auth_item["execution_mode"] == "live_proof"
    assert auth_item["live_proof_accepted"] is False
    acceptance_errors = auth_item["details"]["acceptance_errors"]["errors"]
    assert "proof_type_mismatch" in acceptance_errors
    assert "surface_mismatch" in acceptance_errors
    assert _summary(root)["final_production_readiness_claimed"] is False

    receipts = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")
    items = (root / "m19" / "readiness" / "readiness_items.json").read_text(encoding="utf-8")
    summary = (root / "m19" / "readiness" / "summary.json").read_text(encoding="utf-8")
    for artifact in (receipts, items, summary, stdout):
        for raw_secret in (
            "super-secret",
            "token=secret",
            "user:pass@",
            "opaque-live-token",
            "opaque-error-token",
            "opaque-provider-token",
            "opaque-status-token",
            "opaque-scope-token",
        ):
            assert raw_secret not in artifact

    for detailed_artifact in (receipts, items):
        assert "https://idp.example.invalid/auth" in detailed_artifact
        assert "pipeline.retry_run" in detailed_artifact
        assert "models.activate" in detailed_artifact
        assert "users.manage" in detailed_artifact
    assert "proof_type_mismatch" in items
    assert "surface_mismatch" in items


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


@pytest.mark.parametrize("proof_key", DEPENDENCY_PROOF_PARAMS)
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


def test_scheduler_slurm_status_synced_evidence_is_review_only_not_live_binding(tmp_path: Path) -> None:
    payload = _scheduler_evidence_payload(
        status="slurm_status_synced",
        execution_mode="slurm_status_sync",
        execution_boundary="slurm_status_sync",
        slurm_status_sync_proof={
            "status": "synced",
            "sync_called": True,
            "mutation_occurred": True,
            "protected_by_pre_execution_evidence": True,
            "updated_job_count": 1,
            "terminal_update_count": 1,
        },
        no_mutation_proof={
            "adapter_download_called": False,
            "slurm_submit_called": False,
            "slurm_status_sync_called": True,
            "slurm_cancellation_called": False,
            "shud_runtime_called": False,
            "hydro_result_table_writes": False,
            "met_result_table_writes": False,
            "pipeline_status_writes": True,
            "pipeline_event_writes": True,
        },
    )

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "passed"
    assert scheduler_item["details"]["scheduler_status"] == "slurm_status_synced"
    assert scheduler_item["details"]["execution_boundary"] == "slurm_status_sync"
    assert scheduler_item["details"]["acceptance_errors"] == []
    assert live_item["status"] == "release_blocked"
    errors = live_item["details"]["acceptance_errors"]["errors"]
    assert "scheduler_execution_mode_not_live_eligible" in errors
    assert "scheduler_status_not_live_eligible" in errors
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("proof_field", "proof_value"),
    [
        ("pipeline_status_writes", True),
        ("pipeline_event_writes", True),
        ("pipeline_status_writes", "unknown_after_attempt"),
        ("pipeline_event_writes", "unknown_after_attempt"),
    ],
)
def test_scheduler_dry_run_blocks_pipeline_write_no_mutation_proof_drift(
    tmp_path: Path,
    proof_field: str,
    proof_value: bool | str,
) -> None:
    no_mutation_proof: dict[str, bool | str] = {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "slurm_status_sync_called": False,
        "slurm_cancellation_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
        "pipeline_status_writes": False,
        "pipeline_event_writes": False,
    }
    no_mutation_proof[proof_field] = proof_value
    payload = _scheduler_evidence_payload(
        status="planned",
        execution_mode="dry_run",
        no_mutation_proof=no_mutation_proof,
    )

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert "dry_run_no_mutation_proof_missing" in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_slurm_status_sync_failed_evidence_is_blocked_review_evidence(
    tmp_path: Path,
) -> None:
    payload = _scheduler_evidence_payload(
        status="slurm_status_sync_failed",
        execution_mode="slurm_status_sync",
        execution_boundary="slurm_status_sync",
        candidates=[],
        skipped_candidates=[
            {
                "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
                "source_id": "gfs",
                "cycle_time_utc": "2026-05-21T06:00:00Z",
                "model_id": "model_a",
                "scenario_id": "forecast_gfs_deterministic",
                "run_id": "fcst_gfs_2026052106_model_a",
                "forcing_version_id": "forc_gfs_2026052106_model_a",
                "reason": "active_slurm_status_sync_failed",
                "sync_attempted": True,
                "mutation_outcome": "unknown_after_attempt",
            }
        ],
        counts={
            "candidate_count": 1,
            "blocked_candidate_count": 0,
            "skipped_candidate_count": 1,
            "selected_model_count": 1,
            "source_cycle_count": 1,
            "submitted_count": 0,
            "failed_count": 0,
            "partial_count": 0,
            "slurm_status_sync_count": 0,
            "slurm_status_sync_unknown_count": 1,
        },
        slurm_status_sync_proof={
            "status": "failed",
            "sync_called": True,
            "mutation_outcome": "unknown_after_attempt",
            "mutation_occurred": "unknown_after_attempt",
            "protected_by_pre_execution_evidence": True,
            "failed_sync_count": 1,
            "pipeline_status_writes_proven_absent": False,
            "pipeline_event_writes_proven_absent": False,
        },
        no_mutation_proof={
            "adapter_download_called": False,
            "slurm_submit_called": False,
            "slurm_status_sync_called": True,
            "slurm_cancellation_called": False,
            "shud_runtime_called": False,
            "hydro_result_table_writes": False,
            "met_result_table_writes": False,
            "pipeline_status_writes": "unknown_after_attempt",
            "pipeline_event_writes": "unknown_after_attempt",
        },
    )

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["details"]["scheduler_status"] == "slurm_status_sync_failed"
    assert scheduler_item["details"]["execution_boundary"] == "slurm_status_sync"
    assert scheduler_item["details"]["acceptance_errors"] == []
    assert live_item["status"] == "release_blocked"
    errors = live_item["details"]["acceptance_errors"]["errors"]
    assert "missing_scheduler_evidence_binding" in errors
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


def test_live_proof_receipts_artifact_omits_raw_payload_and_redacts(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    payload = json.loads(_bound_proof("alert", status="failed", accepted=False))
    payload["sink_metadata"]["webhook_url"] = "https://user:pass@alerts.example.invalid/hook?token=secret"
    payload["local_path"] = "/tmp/private-live-proof/receipt.json"

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            alert_proof=json.dumps(payload),
        )
    )

    artifact = json.loads((root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8"))
    rendered = json.dumps(artifact, sort_keys=True)
    assert artifact["schema"] == "nhms.production_readiness.live_proof_receipts.v1"
    assert artifact["run_id"] == "m19"
    assert set(artifact["receipts"]) == set(readiness_shared_artifacts.PROOF_ENV)
    assert set(artifact["redaction"]) == {
        "secrets_redacted",
        "local_paths_redacted",
        "payload_depth_bounded",
        "payload_size_bounded",
    }
    assert artifact["redaction"]["local_paths_redacted"] is True
    assert artifact["redaction"]["payload_depth_bounded"] is True
    assert artifact["redaction"]["payload_size_bounded"] is True
    assert "raw_payload" not in rendered
    assert "token=secret" not in rendered
    assert "user:pass@" not in rendered
    assert "/tmp/private-live-proof/receipt.json" not in rendered
    assert "[redacted" in rendered
    assert len(rendered.encode("utf-8")) < 64 * 1024


def test_live_proof_file_receipt_still_drives_live_item_semantics(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    proof_file = tmp_path / "private" / "alert-proof.json"
    proof_file.parent.mkdir()
    proof_file.write_text(_bound_proof("alert"), encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            alert_proof_file=proof_file,
        )
    )

    item = next(item for item in _items(root) if item["surface"] == "live_alert_sink_delivery")
    preflight = json.loads((root / "m19" / "readiness" / "preflight.json").read_text(encoding="utf-8"))
    artifact_text = (root / "m19" / "readiness" / "live_proof_receipts.json").read_text(encoding="utf-8")

    assert item["status"] == "passed"
    assert item["execution_mode"] == "live_proof"
    assert item["live_proof_accepted"] is True
    assert preflight["live_proof_configured"]["alert"] is True
    assert str(proof_file) not in artifact_text
    assert "private" not in artifact_text
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_shared_artifact_environment_artifact_uses_allowlist_and_redacts_env_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "artifacts"
    private_proof_path = tmp_path / "private" / "auth-proof.json"
    allowed_keys = {
        "NHMS_RUN_PRODUCTION_CLOSURE",
        *readiness_validation.DEPENDENCY_ROOT_ENV.values(),
        readiness_validation.SCHEDULER_EVIDENCE_ROOT_ENV,
        readiness_validation.SCHEDULER_EVIDENCE_FILE_ENV,
        *readiness_validation.PROOF_FILE_ENV.values(),
        "AUTH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "DATABASE_URL",
    }
    monkeypatch.setenv("AUTH_TOKEN", "token=secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "super-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:password@db.internal:55432/nhms")
    monkeypatch.setenv("NHMS_PRODUCTION_READINESS_AUTH_PROOF_FILE", str(private_proof_path))
    dependency_roots = {
        name: tmp_path / "private" / f"{name}-evidence"
        for name in readiness_validation.DEPENDENCY_ROOT_ENV
    }
    for name, private_root in dependency_roots.items():
        monkeypatch.setenv(readiness_validation.DEPENDENCY_ROOT_ENV[name], str(private_root))
    scheduler_root = tmp_path / "private" / "scheduler-root"
    scheduler_file = scheduler_root / "pass.json"
    monkeypatch.setenv(readiness_validation.SCHEDULER_EVIDENCE_ROOT_ENV, str(scheduler_root))
    monkeypatch.setenv(readiness_validation.SCHEDULER_EVIDENCE_FILE_ENV, str(scheduler_file))
    monkeypatch.setenv("UNRELATED_SECRET_TOKEN", "do-not-capture")

    validate_readiness(ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19"))

    artifact = json.loads((root / "m19" / "readiness" / "environment.json").read_text(encoding="utf-8"))
    rendered = json.dumps(artifact, sort_keys=True)
    assert artifact["schema"] == "nhms.production_readiness.environment.v1"
    assert artifact["run_id"] == "m19"
    assert set(artifact["env"]).issubset(allowed_keys)
    for name in readiness_validation.DEPENDENCY_ROOT_ENV:
        assert readiness_validation.DEPENDENCY_ROOT_ENV[name] in artifact["env"]
    assert readiness_validation.SCHEDULER_EVIDENCE_ROOT_ENV in artifact["env"]
    assert readiness_validation.SCHEDULER_EVIDENCE_FILE_ENV in artifact["env"]
    assert "UNRELATED_SECRET_TOKEN" not in artifact["env"]
    assert "do-not-capture" not in rendered
    assert "token=secret" not in rendered
    assert "super-secret" not in rendered
    assert "password" not in rendered
    assert str(private_proof_path) not in rendered
    for private_root in dependency_roots.values():
        assert str(private_root) not in rendered
    assert str(scheduler_root) not in rendered
    assert str(scheduler_file) not in rendered
    assert "[redacted" in rendered


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
        canonical_readiness_provider=ReadyCanonicalReadinessProvider(),
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
        canonical_readiness_provider=ReadyCanonicalReadinessProvider(),
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
        canonical_readiness_provider=ReadyCanonicalReadinessProvider(),
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
    ("container", "embedded_status"),
    [
        ("stage_statuses", [{"stage": "forcing", "status": "submission_failed"}]),
        ("stage_evidence", [{"stage": "forcing", "state": "permanently_failed"}]),
        ("task_results", [{"task_id": "forcing-0", "status": "cancelled"}]),
        ("task_results_summary.status_counts", {"failed": 1, "succeeded": 0}),
        ("stage_statuses.task_results_summary.status_counts", {"unavailable": 1, "succeeded": 0}),
    ],
)
def test_scheduler_live_submitted_embedded_stage_task_failure_status_blocks_live_receipt(
    tmp_path: Path,
    container: str,
    embedded_status: object,
) -> None:
    payload = _submitted_scheduler_payload()
    row = payload["model_run_evidence"][0]
    if container == "task_results_summary.status_counts":
        row["task_results_summary"] = {"status_counts": embedded_status}
    elif container == "stage_statuses.task_results_summary.status_counts":
        row["stage_statuses"] = [{"stage": "forcing", "task_results_summary": {"status_counts": embedded_status}}]
    else:
        row[container] = embedded_status

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "live_status_model_run_blocked_outcome" in errors
    assert "submitted_status_model_run_status_mismatch" in errors
    assert live_item["status"] == "release_blocked"
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


def test_scheduler_live_submitted_embedded_successful_stage_task_statuses_keep_live_receipt_compatible(
    tmp_path: Path,
) -> None:
    payload = _submitted_scheduler_payload()
    payload["model_run_evidence"][0] |= {
        "stage_statuses": [{"stage": "forcing", "status": "succeeded"}],
        "stage_evidence": [{"stage": "forecast", "result": "successful"}],
        "task_results": [
            {"task_id": "forcing-0", "status": "succeeded"},
            {"task_id": "forecast-0", "state": "successful"},
        ],
        "task_results_summary": {
            "status_counts": {
                "succeeded": 2,
                "failed": 0,
                "submission_failed": 0,
                "cancelled": 0,
                "unavailable": 0,
            }
        },
    }

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "passed"
    assert scheduler_item["details"]["acceptance_errors"] == []
    assert live_item["status"] == "passed"
    assert live_item["execution_mode"] == "live_proof"
    assert live_item["live_proof_accepted"] is True
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


@pytest.mark.parametrize("pass_status", ["submission_failed", "permanently_failed"])
def test_scheduler_pass_level_failed_aliases_are_stable_blocked_evidence(
    tmp_path: Path,
    pass_status: str,
) -> None:
    payload = _failed_scheduler_payload(status=pass_status)
    payload["status"] = pass_status

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    errors = scheduler_item["details"]["acceptance_errors"]
    assert scheduler_item["status"] == "blocked"
    assert "status_not_allowed" not in errors
    assert errors == []
    assert scheduler_item["details"]["failed_count"] == 1
    assert scheduler_item["required_for_final"] is False
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


def test_scheduler_root_and_file_configuration_is_stable_blocked_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler-root"
    scheduler_path = tmp_path / "scheduler-file" / "scheduler_20260521120000_fixed.json"
    scheduler_root.mkdir()
    _write_scheduler_evidence(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(
            evidence_root=root,
            run_id="m19",
            scheduler_evidence_root=scheduler_root,
            scheduler_evidence_file=scheduler_path,
        )
    )

    scheduler_items = [item for item in _items(root) if item["surface"] == "scheduler_production_like_evidence"]
    assert len(scheduler_items) == 1
    assert scheduler_items[0]["status"] == "blocked"
    assert scheduler_items[0]["details"]["error_code"] == "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_AMBIGUOUS"
    live_item = next(item for item in _items(root) if item["surface"] == "live_scheduler_evidence_proof")
    assert live_item["status"] == "release_blocked"
    assert live_item["live_proof_accepted"] is False
    assert _summary(root)["final_production_readiness_claimed"] is False


def test_scheduler_root_entry_limit_bounds_non_json_directory_scans(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler"
    scheduler_root.mkdir()
    for index in range(257):
        (scheduler_root / f"ignored_{index:03d}.txt").write_text("ignored", encoding="utf-8")
    _write_scheduler_evidence(scheduler_root / "scheduler_20260521120000_fixed.json")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_root=scheduler_root)
    )

    _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
        root,
        "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_ROOT_ENTRY_LIMIT",
        expected_in="error_code",
    )


def test_scheduler_missing_root_is_stable_blocked_discovery_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "missing-scheduler-root"

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_root=scheduler_root)
    )

    _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
        root,
        "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_DISCOVERY_FAILED",
        expected_in="error_code",
    )


def test_scheduler_empty_root_is_stable_blocked_missing_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "scheduler"
    scheduler_root.mkdir()

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_root=scheduler_root)
    )

    _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
        root,
        "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_MISSING",
        expected_in="error_code",
    )


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
    ("mutator", "expected_error"),
    [
        (lambda payload: payload.pop("pass_id"), "missing_pass_id"),
        (lambda payload: payload.__setitem__("status", "unknown_after_secret"), "status_not_allowed"),
        (
            lambda payload: payload.__setitem__("execution_mode", "live_unreviewed_side_effect"),
            "execution_mode_not_review_evidence",
        ),
        (lambda payload: payload["counts"].pop("candidate_count"), "missing_candidate_count"),
        (lambda payload: payload["counts"].pop("blocked_candidate_count"), "missing_blocked_candidate_count"),
        (lambda payload: payload["counts"].pop("skipped_candidate_count"), "missing_skipped_candidate_count"),
        (lambda payload: payload["counts"].pop("submitted_count"), "missing_submitted_count"),
        (lambda payload: payload["counts"].__setitem__("candidate_count", -1), "negative_counts"),
    ],
)
def test_scheduler_basic_contract_errors_block_evidence_and_live_binding(
    tmp_path: Path,
    mutator: object,
    expected_error: str,
) -> None:
    payload = _scheduler_evidence_payload()
    mutator(payload)

    summary, scheduler_item, live_item = _validate_scheduler_payload_with_matching_live_proof(tmp_path, payload)

    assert scheduler_item["status"] == "blocked"
    assert scheduler_item["execution_mode"] == "not_executed"
    assert scheduler_item["live_proof_accepted"] is False
    assert expected_error in scheduler_item["details"]["acceptance_errors"]
    assert live_item["status"] == "release_blocked"
    assert live_item["live_proof_accepted"] is False
    assert "missing_scheduler_evidence_binding" in live_item["details"]["acceptance_errors"]["errors"]
    assert summary["final_production_readiness_claimed"] is False


@pytest.mark.parametrize(
    ("writer", "expected_error_code"),
    [
        (
            lambda path: path.write_text(json.dumps([_scheduler_evidence_payload()]), encoding="utf-8"),
            "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_JSON_INVALID",
        ),
        (lambda path: path.write_bytes(b"\xff\xfe"), "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED"),
        (lambda path: None, "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED"),
        (lambda path: path.mkdir(parents=True), "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED"),
    ],
)
def test_scheduler_safe_read_boundaries_are_stable_blocked_evidence(
    tmp_path: Path,
    writer: object,
    expected_error_code: str,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "private" / "scheduler" / "scheduler_20260521120000_fixed.json"
    scheduler_path.parent.mkdir(parents=True)
    writer(scheduler_path)

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    scheduler_item, _live_item = _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
        root,
        expected_error_code,
        expected_in="error_code",
    )
    rendered = json.dumps(scheduler_item, sort_keys=True)
    assert str(scheduler_path.parent) not in rendered


@pytest.mark.parametrize("leaf", [True, False])
def test_scheduler_symlink_evidence_boundaries_are_stable_blocked_evidence(
    tmp_path: Path,
    leaf: bool,
) -> None:
    root = tmp_path / "artifacts"
    scheduler_root = tmp_path / "private" / "scheduler"
    real_root = tmp_path / "private" / "real-scheduler"
    real_path = real_root / "scheduler_20260521120000_fixed.json"
    _write_scheduler_evidence(real_path)
    if leaf:
        scheduler_root.mkdir(parents=True)
        scheduler_path = scheduler_root / "scheduler_20260521120000_fixed.json"
        scheduler_path.symlink_to(real_path)
    else:
        scheduler_root.parent.mkdir(parents=True, exist_ok=True)
        scheduler_root.symlink_to(real_root, target_is_directory=True)
        scheduler_path = scheduler_root / "scheduler_20260521120000_fixed.json"

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    scheduler_item, _live_item = _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
        root,
        "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED",
        expected_in="error_code",
    )
    rendered = json.dumps(scheduler_item, sort_keys=True)
    assert str(scheduler_root.parent) not in rendered


def test_scheduler_json_value_error_is_stable_redacted_blocked_evidence(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    scheduler_path = tmp_path / "private" / "scheduler" / "scheduler_20260521120000_fixed.json"
    scheduler_path.parent.mkdir(parents=True)
    payload = json.dumps(_scheduler_evidence_payload())
    oversized_int_json = payload.replace('"candidate_count": 2', '"candidate_count": ' + "9" * 5000)
    scheduler_path.write_text(oversized_int_json, encoding="utf-8")

    validate_readiness(
        ProductionReadinessConfig.from_env(evidence_root=root, run_id="m19", scheduler_evidence_file=scheduler_path)
    )

    scheduler_item, _live_item = _assert_blocked_scheduler_evidence_with_release_blocked_live_item(
        root,
        "PRODUCTION_READINESS_SCHEDULER_EVIDENCE_READ_FAILED",
        expected_in="error_code",
    )
    rendered = json.dumps(scheduler_item, sort_keys=True)
    assert "9999999999" not in rendered
    assert str(scheduler_path.parent) not in rendered


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
    assert all(item["required_for_final"] is False for item in consumed)
    consumed_by_dependency = {item["details"]["dependency"]: item for item in consumed}
    assert set(readiness_validation._dependency_bindings(consumed)) == set(DEPENDENCY_PROOFS)
    for dependency, (issue, schema) in DEPENDENCY_CONTRACTS.items():
        item = consumed_by_dependency[dependency]
        details = item["details"]
        assert details["producer_issue"] == issue
        assert details["producer_schema"] == schema
        assert details["summary_issue"] == issue
        assert details["summary_schema"] == schema
        assert details["summary_status"] == "ready"
        assert details["summary_run_id"] in {f"{dependency}-run", "object-store-run"}
        assert details["producer_artifact_ref"] == f"{dependency}:summary.json"
        assert details["summary_checksum"].startswith("sha256:")
        assert f"issue=#{issue}" in item["dependencies"]
        assert f"schema={schema}" in item["dependencies"]
        assert "summary_status=ready" in item["dependencies"]
        assert f"producer_artifact_ref={dependency}:summary.json" in item["dependencies"]
        assert any(dependency_ref.startswith("summary_checksum=sha256:") for dependency_ref in item["dependencies"])
    assert "live_scheduler_evidence_proof" not in {item["surface"] for item in items}
    assert _summary(root)["final_production_readiness_claimed"] is False
    assert _summary(root)["status"] == "release_blocked"


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
